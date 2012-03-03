# -*- coding: utf-8 -*-

"""
Copyright (C) 2010 Dariusz Suchojad <dsuch at gefira.pl>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
import asyncore, logging, time
from httplib import INTERNAL_SERVER_ERROR, responses
from threading import Thread
from traceback import format_exc

# zope.server
from zope.server.http.httpserver import HTTPServer

# ZeroMQ
import zmq

# Paste
from paste.util.multidict import MultiDict

# Bunch
from bunch import Bunch

# Zato
from zato.broker.zato_client import BrokerClient
from zato.common import PORTS, ZATO_JOIN_REQUEST_ACCEPTED
from zato.common.broker_message import AMQP_CONNECTOR, JMS_WMQ_CONNECTOR, ZMQ_CONNECTOR, MESSAGE_TYPE
from zato.common.util import new_rid
from zato.server.base import BrokerMessageReceiver
from zato.server.base.worker import _HTTPServerChannel, _TaskDispatcher
from zato.server.connection.amqp.channel import start_connector as amqp_channel_start_connector
from zato.server.connection.amqp.outgoing import start_connector as amqp_out_start_connector
from zato.server.connection.ftp import FTPFacade
from zato.server.connection.jms_wmq.channel import start_connector as jms_wmq_channel_start_connector
from zato.server.connection.jms_wmq.outgoing import start_connector as jms_wmq_out_start_connector
from zato.server.connection.zmq_.channel import start_connector as zmq_channel_start_connector
from zato.server.connection.zmq_.outgoing import start_connector as zmq_outgoing_start_connector

logger = logging.getLogger(__name__)

class ZatoHTTPListener(HTTPServer):
    
    SERVER_IDENT = 'Zato'
    channel_class = _HTTPServerChannel
    
    def __init__(self, server, task_dispatcher, broker_client=None):
        self.server = server
        self.broker_client = broker_client
        super(ZatoHTTPListener, self).__init__(self.server.host, self.server.port, 
                                               task_dispatcher)

    def executeRequest(self, task, thread_ctx):
        """ Handles incoming HTTP requests. Each request is being handled by one
        of the threads created in ParallelServer.run_forever method.
        """
        rid = new_rid()
        response = str(rid)
        
        try:
            # SOAP or plain HTTP.
            response = thread_ctx.store.request_handler.handle(rid, task, thread_ctx)

        # Any exception at this point must be our fault.
        except Exception, e:
            tb = format_exc(e)
            task.setResponseStatus(INTERNAL_SERVER_ERROR, responses[INTERNAL_SERVER_ERROR])
            error_msg = '[{0}] Exception caught [{1}]'.format(rid, tb)
            logger.error(error_msg)
            
            response = error_msg
            task.response_headers['Content-Type'] = 'text/plain'    
            
        task.response_headers['X-Zato-RID'] = rid
            
        # Return the HTTP response.
        task.response_headers['Content-Length'] = str(len(response))
        task.write(response)


class ParallelServer(BrokerMessageReceiver):
    def __init__(self, host=None, port=None, zmq_context=None, crypto_manager=None,
                 odb=None, singleton_server=None, worker_config=None, repo_location=None,
                 ftp=None):
        self.host = host
        self.port = port
        self.zmq_context = zmq_context or zmq.Context()
        self.crypto_manager = crypto_manager
        self.odb = odb
        self.singleton_server = singleton_server
        self.worker_config = worker_config
        self.repo_location = repo_location
        self.ftp = ftp
        
    def _after_init_common(self, server):
        """ Initializes parts of the server that don't depend on whether the
        server's been allowed to join the cluster or not.
        """
        
        self.broker_token = server.cluster.broker_token
        self.broker_push_worker_pull = 'tcp://{0}:{1}'.format(server.cluster.broker_host, 
                server.cluster.broker_start_port + PORTS.BROKER_PUSH_WORKER_THREAD_PULL)
        self.worker_push_broker_pull = self.parallel_push_broker_pull = 'tcp://{0}:{1}'.format(server.cluster.broker_host, 
                server.cluster.broker_start_port + PORTS.WORKER_THREAD_PUSH_BROKER_PULL)
        self.broker_pub_worker_sub = 'tcp://{0}:{1}'.format(server.cluster.broker_host, 
                server.cluster.broker_start_port + PORTS.BROKER_PUB_WORKER_THREAD_SUB)
        
        if self.singleton_server:
            
            self.service_store.read_internal_services()
            
            kwargs = {'zmq_context':self.zmq_context,
            'broker_host': server.cluster.broker_host,
            'broker_push_singleton_pull_port': server.cluster.broker_start_port + PORTS.BROKER_PUSH_SINGLETON_PULL,
            'singleton_push_broker_pull_port': server.cluster.broker_start_port + PORTS.SINGLETON_PUSH_BROKER_PULL,
            'broker_token':self.broker_token,
                    }
            Thread(target=self.singleton_server.run, kwargs=kwargs).start()
            
            # Let the scheduler fully initialize
            time.sleep(0.2)
    
    def _after_init_accepted(self, server):
        if self.singleton_server:
            for(_, name, is_active, job_type, start_date, extra, service,\
                _, weeks, days, hours, minutes, seconds, repeats, cron_definition)\
                    in self.odb.get_job_list(server.cluster.id):
                if is_active:
                    job_data = Bunch({'name':name, 'is_active':is_active, 
                        'job_type':job_type, 'start_date':start_date, 
                        'extra':extra, 'service':service,  'weeks':weeks, 
                        'days':days, 'hours':hours, 'minutes':minutes, 
                        'seconds':seconds,  'repeats':repeats, 
                        'cron_definition':cron_definition})
                    self.singleton_server.scheduler.create_edit('create', job_data)

            # Start the connectors only once throughout the whole cluster
            self._init_connectors(server)
            
        # FTP
        ftp_conn_params = Bunch()
        for item in self.odb.get_out_ftp_list(server.cluster.id):
            ftp_conn_params[item.name] = Bunch()
            ftp_conn_params[item.name].is_active = item.is_active
            ftp_conn_params[item.name].name = item.name
            ftp_conn_params[item.name].host = item.host
            ftp_conn_params[item.name].user = item.user
            ftp_conn_params[item.name].password = item.password
            ftp_conn_params[item.name].acct = item.acct
            ftp_conn_params[item.name].timeout = item.timeout
            ftp_conn_params[item.name].port = item.port
            ftp_conn_params[item.name].dircache = item.dircache
            
        self.ftp = FTPFacade(ftp_conn_params)
                    
        self.worker_config = Bunch()
        
        # Repo location so that AMQP subprocesses know how where to read
        # the server's configuration from.
        self.worker_config.repo_location = self.repo_location
        
        # The broker client for each of the worker threads.
        self.worker_config.broker_config = Bunch()
        self.worker_config.broker_config.name = 'worker-thread'
        self.worker_config.broker_config.broker_token = self.broker_token
        self.worker_config.broker_config.zmq_context = self.zmq_context
        self.worker_config.broker_config.broker_push_client_pull = self.broker_push_worker_pull
        self.worker_config.broker_config.client_push_broker_pull = self.worker_push_broker_pull
        self.worker_config.broker_config.broker_pub_client_sub = self.broker_pub_worker_sub
        
        # HTTP Basic Auth
        ba_config = Bunch()
        for item in self.odb.get_basic_auth_list(server.cluster.id):
            ba_config[item.name] = Bunch()
            ba_config[item.name].is_active = item.is_active
            ba_config[item.name].username = item.username
            ba_config[item.name].realm = item.realm
            ba_config[item.name].password = item.password
            
        # Technical accounts
        ta_config = Bunch()
        for item in self.odb.get_tech_acc_list(server.cluster.id):
            ta_config[item.name] = Bunch()
            ta_config[item.name].is_active = item.is_active
            ta_config[item.name].name = item.name
            ta_config[item.name].password = item.password
            ta_config[item.name].salt = item.salt
            
        wss_config = Bunch()
        for item in self.odb.get_wss_list(server.cluster.id):
            wss_config[item.name] = Bunch()
            wss_config[item.name].is_active = item.is_active
            wss_config[item.name].username = item.username
            wss_config[item.name].password = item.password
            wss_config[item.name].password_type = item.password_type
            wss_config[item.name].reject_empty_nonce_creat = item.reject_empty_nonce_creat
            wss_config[item.name].reject_stale_tokens = item.reject_stale_tokens
            wss_config[item.name].reject_expiry_limit = item.reject_expiry_limit
            wss_config[item.name].nonce_freshness_time = item.nonce_freshness_time

        # Security configuration of HTTP URLs.
        url_sec = self.odb.get_url_security(server)
        
        # All the HTTP/SOAP channels.
        http_soap = MultiDict()
        for item in self.odb.get_http_soap_list(server.cluster.id, 'channel'):
            _info = Bunch()
            _info[item.soap_action] = Bunch()
            _info[item.soap_action].id = item.id
            _info[item.soap_action].name = item.name
            _info[item.soap_action].is_internal = item.is_internal
            _info[item.soap_action].url_path = item.url_path
            _info[item.soap_action].method = item.method
            _info[item.soap_action].soap_version = item.soap_version
            _info[item.soap_action].service_id = item.service_id
            _info[item.soap_action].service_name = item.service_name
            _info[item.soap_action].impl_name = item.impl_name
            http_soap.add(item.url_path, _info)
            
        self.worker_config.basic_auth = ba_config
        self.worker_config.tech_acc = ta_config
        self.worker_config.wss = wss_config
        self.worker_config.url_sec = url_sec
        self.worker_config.http_soap = http_soap

        # The parallel server's broker client. The client's used to notify
        # all the server's AMQP subprocesses that they need to shut down.

        self.broker_client = BrokerClient()
        self.broker_client.name = 'parallel'
        self.broker_client.token = server.cluster.broker_token
        self.broker_client.zmq_context = self.zmq_context
        self.broker_client.client_push_broker_pull = self.parallel_push_broker_pull
        
        self.broker_client.init()
        self.broker_client.start()
        
    def _init_connectors(self, server):
        """ Starts all the connector subprocesses.
        """

        # AMQP - channels    
        for item in self.odb.get_channel_amqp_list(server.cluster.id):
            amqp_channel_start_connector(self.repo_location, item.id, item.def_id)
        
        # AMQP - outgoing
        for item in self.odb.get_out_amqp_list(server.cluster.id):
            amqp_out_start_connector(self.repo_location, item.id, item.def_id)
            
        # JMS WMQ - channels
        for item in self.odb.get_channel_jms_wmq_list(server.cluster.id):
            jms_wmq_channel_start_connector(self.repo_location, item.id, item.def_id)
    
        # JMS WMQ - outgoing
        for item in self.odb.get_out_jms_wmq_list(server.cluster.id):
            jms_wmq_out_start_connector(self.repo_location, item.id, item.def_id)
            
        # ZMQ - channels
        for item in self.odb.get_channel_zmq_list(server.cluster.id):
            zmq_channel_start_connector(self.repo_location, item.id)
            
        # ZMQ - outgoimg
        for item in self.odb.get_out_zmq_list(server.cluster.id):
            zmq_outgoing_start_connector(self.repo_location, item.id)
            
    def _after_init_non_accepted(self, server):
        pass    
        
    def after_init(self):
        
        # First try grabbing the basic server's data from the ODB. No point
        # in doing anything else if we can't get past this point.
        server = self.odb.fetch_server()
        
        if not server:
            raise Exception('Server does not exist in the ODB')
        
        self._after_init_common(server)
        
        # A server which hasn't been approved in the cluster still needs to fetch
        # all the config data but it won't start any MQ/AMQP/ZMQ/etc. listeners
        # except for a ZMQ config subscriber that will listen for an incoming approval.
        
        if server.last_join_status == ZATO_JOIN_REQUEST_ACCEPTED:
            self._after_init_accepted(server)
        else:
            msg = 'Server has not been accepted, last_join_status=[{0}]'
            logger.warn(msg.format(server.last_join_status))
            
            self._after_init_non_accepted(server)

    def run_forever(self):
        
        task_dispatcher = _TaskDispatcher(self, self.worker_config, self.on_broker_msg, self.zmq_context)
        task_dispatcher.setThreadCount(10)

        logger.debug('host=[{0}], port=[{1}]'.format(self.host, self.port))

        ZatoHTTPListener(self, task_dispatcher)

        try:
            while True:
                asyncore.poll(5)

        except KeyboardInterrupt:
            logger.info('Shutting down')

            # Close all the connector subprocesses this server has started
            pairs = ((AMQP_CONNECTOR.CLOSE, MESSAGE_TYPE.TO_AMQP_CONNECTOR_SUB),
                    (JMS_WMQ_CONNECTOR.CLOSE, MESSAGE_TYPE.TO_JMS_WMQ_CONNECTOR_SUB),
                    (ZMQ_CONNECTOR.CLOSE, MESSAGE_TYPE.TO_ZMQ_CONNECTOR_SUB),
                    )
            
            for action, msg_type in pairs:
                msg = {}
                msg['action'] = action
                msg['odb_token'] = self.odb.odb_data['token']
                self.broker_client.send_json(msg, msg_type=msg_type)
                time.sleep(0.2)
            
            self.broker_client.close()
            
            if self.singleton_server:
                if getattr(self.singleton_server, 'broker_client', None):
                    self.singleton_server.broker_client.close()
                
            self.zmq_context.term()
            self.odb.close()
            task_dispatcher.shutdown()

# ##############################################################################