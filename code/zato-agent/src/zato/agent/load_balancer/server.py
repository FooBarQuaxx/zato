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
import httplib, json, logging, logging.config, os, ssl, urllib
from subprocess import Popen, PIPE
from tempfile import NamedTemporaryFile
from time import sleep, time
from traceback import format_exc

# Spring Python
from springpython.remoting.xmlrpc import SSLServer

# Zato
from zato.agent.load_balancer.config import config_from_string, string_from_config
from zato.agent.load_balancer.haproxy_stats import HAProxyStats
from zato.common import ZATO_OK
from zato.common.haproxy import haproxy_stats
from zato.common.util import TRACE1

public_method_prefix = "_lb_agent_"
config_file = "zato.config"

logger = logging.getLogger("")
logging.addLevelName('TRACE1', TRACE1)

# All known HAProxy commands
haproxy_commands = {}
for version, commands in haproxy_stats.items():
    haproxy_commands.update(commands)

# We'll wait up to that many seconds for HAProxy to validate the config file.
HAPROXY_VALIDATE_TIMEOUT=0.3

class LoadBalancerAgent(SSLServer):
    def __init__(self, json_config_path):

        config_dir = os.path.dirname(json_config_path)
        self.json_config = json.loads(open(json_config_path).read())

        self.work_dir = os.path.abspath(os.path.join(config_dir, self.json_config['work_dir']))
        self.haproxy_command = self.json_config['haproxy_command']
        self.verify_fields = self.json_config['verify_fields']

        self.keyfile = os.path.abspath(os.path.join(config_dir, self.json_config['keyfile']))
        self.certfile = os.path.abspath(os.path.join(config_dir, self.json_config['certfile']))
        self.ca_certs = os.path.abspath(os.path.join(config_dir, self.json_config['ca_certs']))

        log_config = os.path.abspath(os.path.join(config_dir, self.json_config['log_config']))
        work_dir = os.path.abspath(os.path.join(config_dir, self.json_config['work_dir']))
        haproxy_command = self.json_config['haproxy_command']

        logging.config.fileConfig(log_config)

        self.work_dir = os.path.abspath(work_dir)
        self.haproxy_command = haproxy_command
        self.config_path = os.path.join(self.work_dir, config_file)
        self.config = self._read_config()
        self.start_time = time()
        self.haproxy_stats = HAProxyStats(self.config.global_["stats_socket"])

        super(LoadBalancerAgent, self).__init__(host=self.json_config['host'],
                port=self.json_config['port'], keyfile=self.keyfile, certfile=self.certfile,
                ca_certs=self.ca_certs, cert_reqs=ssl.CERT_REQUIRED,
                verify_fields=self.verify_fields)

    def _dispatch(self, method, params):
        try:
            return SSLServer._dispatch(self, method, params)
        except Exception, e:
            logger.error(format_exc(e))
            raise e

    def register_functions(self):
        """ All methods with the '_lb_agent_' prefix will be exposed through
        SSL XML-RPC after chopping off the prefix, so that self._lb_agent_ping
        becomes a 'ping' method, self._lb_agent_get_uptime_info -> 'get_uptime_info'
        etc.
        """
        for item in dir(self):
            if item.startswith(public_method_prefix):
                public_name = item.split(public_method_prefix)[1]
                attr = getattr(self, item)
                msg = "Registering [{attr}] under public name [{public_name}]"
                logger.debug(msg.format(attr=attr, public_name=public_name)) # TODO: Add logging config
                self.register_function(attr, public_name)

    def _read_config(self):
        """ Read and parse the HAProxy configuration.
        """
        data = open(self.config_path).read()
        return config_from_string(data)

    def _validate(self, config_string):

        try:
            with NamedTemporaryFile(prefix="zato-tmp", dir=self.work_dir) as tf:

                tf.write(config_string)
                tf.flush()

                command = [self.haproxy_command, "-c", "-f", tf.name]
                p = Popen(command, stdout=PIPE, stderr=PIPE)

                # Build it up front here, who knows, maybe we'll need it and if we
                # do it may be needed in several places.
                common_error_details = "command=[{command}], config_file=[{config_file}]"
                common_error_details = common_error_details.format(command=command, config_file=open(tf.name).read())

                sleep(HAPROXY_VALIDATE_TIMEOUT)
                p.poll()

                # returncode can be 0 (and we actually hope it is :-))
                if p.returncode is None:
                    msg = "HAProxy didn't respond in [{HAPROXY_VALIDATE_TIMEOUT}] seconds. "
                    msg += common_error_details
                    msg = msg.format(HAPROXY_VALIDATE_TIMEOUT=HAPROXY_VALIDATE_TIMEOUT)
                    raise Exception(msg)
                else:
                    # returncode not being equal to 0 means there were problems with
                    # validating the config file, stdout & stderr will have details.
                    if p.returncode != 0:
                        stdout, stderr = p.communicate()
                        msg = "Failed to validate the config file using HAProxy. "
                        msg += "return code=[{returncode}], stdout=[{stdout}], stderr=[{stderr}] "
                        msg += common_error_details
                        msg = msg.format(returncode=p.returncode, stdout=stdout, stderr=stderr)
                        raise Exception(msg)

                # All went fine, config was valid.
                return True
        except Exception, e:
            msg = "Caught an exception, e=[{e}]".format(e=format_exc(e))
            logger.error(msg)
            raise Exception(msg)

    def _save_config(self, config_string):
        """ Save a new HAProxy config file on disk. It is assumed the file
        has already been validated.
        """
        # TODO: Use local bzr repo here
        f = open(self.config_path, "wb")
        f.write(config_string)
        f.close()

        self.config = self._read_config()

    def _validate_save_config_string(self, config_string, save):
        """ Given a string representing the HAProxy config file it first validates
        it and then optionally saves it.
        """
        self._validate(config_string)

        if save:
            self._save_config(config_string)

        return True

# ##############################################################################

    def _lb_agent_validate_save_source_code(self, source_code, save=False):
        """ Validate or validates & saves (if 'save' flag is True) an HAProxy
        configuration passed in as a string. Note that the validation step is always performed.
        """
        return self._validate_save_config_string(source_code, save)

    def _lb_agent_validate_save(self, lb_config, save=False):
        """ Validate or validates & saves (if 'save' flag is True) an HAProxy
        configuration. Note that the validation step is always performed.
        """
        config_string = string_from_config(lb_config, open(self.config_path).readlines())
        return self._validate_save_config_string(config_string, save)

    def _lb_agent_get_servers_state(self):
        """ Return a three-key dictionary describing the current state of all Zato servers
        as seen by HAProxy. Keys are "UP" for running servers, "DOWN" for those
        that are unavailable, and "MAINT" for servers in the maintenance mode.
        Values are dictionaries of access type -> names of servers. For instance,
        if there are three servers, one is UP, the second one is DOWN and the
        third one is MAINT, the result will be:

        {
          "UP": {"http_plain": ["SERVER.1"]},
          "DOWN": {"http_plain": ["SERVER.2"]},
          "MAINT": {"http_plain": ["SERVER.3"]},
        }
        """

        servers_state = {
            "UP": {"http_plain":[]},
            "DOWN": {"http_plain":[]},
            "MAINT": {"http_plain":[]},
        }
        stat = self.haproxy_stats.execute("show stat")

        for line in stat.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            line = line.split(",")

            haproxy_name = line[0]
            haproxy_type_or_name = line[1]

            if haproxy_name.startswith("bck") and not haproxy_type_or_name == "BACKEND":
                backend_name, state = line[1], line[17]
                access_type, server_name = backend_name.split("--")

                # Don't bail out when future HAProxy versions introduce states
                # we aren't currently aware of.
                if state not in servers_state:
                    msg = "Encountered unknown state [{state}], recognized ones are [{states}]"
                    logger.warning(msg.format(state=state, states=str(sorted(servers_state))))
                else:
                    servers_state[state][access_type].append(server_name)

        return servers_state

    def _lb_agent_execute_command(self, command, timeout, extra=""):
        """ Execute an HAProxy command through its UNIX socket interface.
        """
        command = haproxy_commands[int(command)][0]
        timeout = int(timeout)

        result = self.haproxy_stats.execute(command, extra, timeout)

        # Special-case the request for describing the commands available.
        # There's no 'describe commands' command in HAProxy but HAProxy is
        # nice enough to return a usage info when it encounters an unknown
        # command which we parse and return to the caller.
        if command == "ZATO_DESCRIBE_COMMANDS":
            result = "\n\n" + "\n".join(result.splitlines()[1:])

        return result

    def _lb_agent_haproxy_version_info(self):
        """ Return a three-element tuple describing HAProxy's version,
        similar to what stdlib's sys.version_info does.
        """

        # 'show info' is always available and we use it for determining the HAProxy version.
        info = self.haproxy_stats.execute("show info")
        for line in info.splitlines():
            if line.startswith("Version:"):
                version = line.split("Version:")[1]
                return version.strip().split(".")

    def _lb_agent_ping(self):
        """ Always return ZATO_OK.
        """
        return ZATO_OK

    def _lb_agent_get_config(self):
        """ Return those pieces of an HAProxy configuration that are understood
        by Zato.
        """
        return self.config

    def _lb_agent_get_config_source_code(self):
        """ Return the HAProxy configuration file's source.
        """
        return open(self.config_path).read()

    def _lb_agent_get_uptime_info(self):
        """ Return the agent's (not HAProxy's) uptime info, currently returns
        only the time it was started at.
        """
        return self.start_time

    def _lb_agent_is_haproxy_alive(self):
        """ Invoke HAProxy through HTTP monitor_uri and return ZATO_OK if
        HTTP status code is 200. Raise Exception otherwise.
        """
        host = self.config.frontend["front_http_plain"]["bind"]["address"]
        port = self.config.frontend["front_http_plain"]["bind"]["port"]
        path = self.config.frontend["front_http_plain"]["monitor_uri"]
        url = "http://{host}:{port}{path}".format(host=host, port=port, path=path)

        try:
            conn = urllib.urlopen(url)
        except Exception, e:
            msg = "Could not open URL [{url}], e=[{e}]".format(url=url, e=format_exc(e))
            logger.error(msg)
            raise Exception(msg)
        else:
            try:
                code = conn.getcode()
                if code  == httplib.OK:
                    return ZATO_OK
                else:
                    msg = "Could not open URL [{url}], HTTP code=[{code}]".format(url=url, code=code)
                    logger.error(msg)
                    raise Exception(msg)
            finally:
                conn.close()

    def _lb_agent_get_work_config(self):
        """ Return the agent's basic configuration.
        """
        return {"work_dir":self.work_dir, "haproxy_command":self.haproxy_command,
                "keyfile":self.keyfile, "certfile":self.certfile,
               "ca_certs":self.ca_certs, "verify_fields":self.verify_fields}