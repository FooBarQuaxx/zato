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

""" Manages the server's SQL connection pools.
"""

# stdlib
from copy import deepcopy
from logging import getLogger
from threading import RLock
from time import time

# SQLAlchemy
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool

# validate
from validate import is_boolean, is_integer, VdtTypeError

# Spring Python
from springpython.context import DisposableObject

# Zato
from zato.common.odb import engine_def, ping_queries

class SQLConnectionPool(object):
    def __init__(self, name, data, data_no_sensitive):
        self.logger = getLogger(self.__class__.__name__)
        self.name = name
        self.data = data
        
        # Safe for printing out to logs, any sensitive data has been shadowed
        self.data_no_sensitive = data_no_sensitive 
        
        _extra = {}
        for line in self.data.get('extra', '').splitlines():
            original_line = line
            if line:
                line = line.split('=')
                if not len(line) == 2:
                    raise ValueError('Each line must be a single key=value entry, not [{}]'.format(original_line))
                
                key, value = line
                value = value.strip()
                
                try:
                    value = is_boolean(value)
                except VdtTypeError:
                    # It's cool, not a boolean
                    pass 
                
                try:
                    value = is_integer(value)
                except VdtTypeError:
                    # OK, not an integer
                    pass 
                
                _extra[key.strip()] = value
        
        engine_url = engine_def.format(**data)
        self.engine = create_engine(engine_url, pool_size=int(data['pool_size']), **_extra)
        
    def ping(self):
        """ Pings the SQL database and returns the response time, in milliseconds.
        """
        query = ping_queries[self.engine.name]

        self.logger.debug('About to ping the SQL connection pool:[{}], query:[{}]'.format(self.data_no_sensitive, query))

        start_time = time()
        self.engine.connect().execute(query)
        response_time = time() - start_time

        self.logger.debug('Ping OK, pool:[{0}], response_time:[{1:03.4f} s]'.format(self.data_no_sensitive, response_time))

        return response_time


class PoolStore(DisposableObject):
    """ A main class for accessing all of the SQL connection pools. Each server
    thread has its own store.
    """
    def __init__(self):
        super(PoolStore, self).__init__()
        self._lock = RLock()
        self.pools = {}
        
    def __getitem__(self, name):
        """ Checks out the connection pool.
        """
        with self._lock:
            return self.pools[name]
        
    def __setitem__(self, name, data):
        """ Stops a connection pool if it exists and replaces it with a new one 
        using updated settings. 'is_odb' is a flag used for creating a new ODB
        connection for the server instead of a regular user-defined SQL connection
        pool.
        """
        with self._lock:
            data_no_sensitive = deepcopy(data)
            data_no_sensitive['password'] = '***'
            pool = SQLConnectionPool(name, data, data_no_sensitive)
            self.pools[name] = pool
    
    def __delitem__(self, name):
        """ Stops a pool and deletes it from the store.
        """
        with self._lock:
            del self.pools[name]
        
    def destroy(self):
        """ Invoked when Spring Python's container is releasing the store.
        """
        with self._lock:
            for name, pool in self.pools.items():
                pool.engine.dispose()