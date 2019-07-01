#!/usr/bin/env python3
"""Talk with the backend, communicate with local logic via ZMQ"""
import json
import logging
import os
import sys

import ardubus_core
import zmq

from helpers import log_exceptions

FRAMEWORK_UPDATE_FPS = 15  # How often to call updates

# This is F-UGLY but can't be helped, the framework is not packaged properly
sys.path.append(os.path.realpath(os.path.join(os.path.dirname(__file__), '..')))  # isort:skip
from odysseus import log  # noqa: F401 ; isort:skip  ; # pylint: disable=C0413,W0611,E0401,C0411
from odysseus.taskbox import TaskBoxRunner  # isort:skip ; # pylint: disable=C0413,E0401,C0411


class BackendComs:
    """Talk with the backend, communicate with local logic via ZMQ"""

    def __init__(self):
        # init standard logging
        ardubus_core.init_logging(logging.INFO)
        self.logger = logging.getLogger('reactorconsole:backendcoms')
        self.zmq_ctx = zmq.Context()
        self.zmq_pub_socket = self.zmq_ctx.socket(zmq.PUB)  # pylint: disable=E1101
        self.zmq_sub_socket = self.zmq_ctx.socket(zmq.SUB)  # pylint: disable=E1101

    @log_exceptions
    def framework_init(self):
        """Called by the odysseys framework on init"""
        self.logger.debug('Binding/Connecting the ZMQ sockets')
        self.zmq_pub_socket.bind('ipc:///tmp/reactor_backend.zmq')
        self.zmq_sub_socket.connect('ipc:///tmp/reactor_locallogic.zmq')
        self.zmq_sub_socket.subscribe(b'local2backend')
        self.zmq_sub_socket.subscribe(b'staterequest')
        self.zmq_sub_socket.setsockopt(zmq.RCVTIMEO, 100)  # pylint: disable=E1101

    @log_exceptions
    def framework_update(self, state, backend_changed):
        """Called by the odysseys framework periodically"""
        # self.logger.debug('called with state: {}'.format(repr(state)))
        force_state_send = False
        local_state_received = False

        try:
            ret = self.zmq_sub_socket.recv_multipart()
            self.logger.debug('Got ZMQ parts: {}'.format(ret))
            if ret[0] == b'staterequest':
                force_state_send = True
            elif ret[0] == b'local2backend':
                local_state_received = True
                state = json.loads(ret[1].decode('utf-8'))
        except zmq.Again:
            # No new messages from backend
            pass

        if (backend_changed or force_state_send) and state:
            jsonstr = json.dumps(state, ensure_ascii=False)
            self.zmq_pub_socket.send_multipart([b'backend2local', jsonstr.encode('utf-8')])
            if backend_changed:
                self.logger.info('Pushed new state from backend to ZMQ')
            else:
                self.logger.debug('Force-Pushed old state from backend to ZMQ')
            self.logger.debug('Pushed state was {}'.format(repr(state)))

        if local_state_received:
            return state
        return None


if __name__ == '__main__':
    BACKENDCOMS = BackendComs()
    # Set debug only for our local logger
    # BACKENDCOMS.logger.setLevel(logging.DEBUG)
    # Since the framework does not provide callbacks for clean shutdowns we must use ataxit as last resort
    TASK_OPTIONS = {
        'callback': BACKENDCOMS.framework_update,
        'init': BACKENDCOMS.framework_init,
        'run_interval': 1.0 / FRAMEWORK_UPDATE_FPS,
    }
    # This will run forever
    TaskBoxRunner(TASK_OPTIONS).run()
