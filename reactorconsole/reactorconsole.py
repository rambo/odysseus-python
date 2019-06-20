#!/usr/bin/env python3
"""Local logic for the "reactor console" """
import atexit
import logging
import os
import sys
import time

import ardubus_core
import ardubus_core.deviceconfig
import ardubus_core.transport
from ardubus_core.aiowrapper import AIOWrapper

# This is F-UGLY but can't be helped, the framework is not packaged properly
sys.path.append(os.path.realpath(os.path.join(os.path.dirname(__file__), '..')))  # isort:skip
from odysseus import log  # noqa: F401 ; isort:skip  ; # pylint: disable=C0413,W0611,E0401
from odysseus.taskbox import TaskBoxRunner  # isort:skip ; # pylint: disable=C0413,E0401

UPDATE_FPS = 25  # How often to call updates
FORCE_UPDATE_INTERVAL = 1.0  # How often to force-update all states to HW


class ReactorState:
    """Keep track of the state without using fugly global variables"""
    ardubus_devicename = 'rod_control_panel'
    serialpath = None
    devicesyml_path = None
    aliases = None
    ardubus_transport = None
    gauge_directions = None
    gauge_values = None
    last_full_update = 0

    def __init__(self, serialpath='/dev/ttyUSB0', devicesyml_path='./ardubus_devices.yml', loglevel=logging.INFO):
        self.serialpath = serialpath
        self.devicesyml_path = devicesyml_path
        self.aliases = None
        self.ardubus_transport = None
        self.gauge_directions = {}
        self.gauge_values = {}
        self.last_full_update = 0

        # init standard logging
        ardubus_core.init_logging(loglevel)
        self.logger = logging.getLogger('reactorconsole')

        self._init_ardubus_transport()

    def _init_ardubus_transport(self):
        """Initialize ardubus transport"""
        self.logger.info("Initializing ardubus")
        ardubus_core.deviceconfig.load_devices_yml(self.devicesyml_path)
        transport_aio = ardubus_core.transport.get(self.serialpath,
                                                   ardubus_core.deviceconfig.FULL_CONFIG_MAP[self.ardubus_devicename])
        transport_aio.events_callback = self.ardubus_callback
        self.ardubus_transport = AIOWrapper(transport_aio)

    def _reset_console_values(self):
        """Reset all console values to default"""
        self.logger.debug('called')
        # TODO: reset the expected state values to power-on defaults
        self._do_full_update()

    def _do_full_update(self):
        """Send all values to HW"""
        self.logger.debug('called')
        # TODO: loop through all the expected states and set them
        self.last_full_update = time.time()

    def ardubus_callback(self, event):
        """Ardubus events callback"""

    def framework_init(self):
        """Called by the odysseys framework on init"""
        self._reset_console_values()

    def framework_update(self, state, backend_change):
        """Called by the odysseys framework periodically"""
        now = time.time()

        # TODO: implement logic

        if (now - self.last_full_update) > FORCE_UPDATE_INTERVAL:
            self._do_full_update()

    def cleanup(self):
        """cleanup on quit"""
        self._reset_console_values()
        self.ardubus_transport.quit()


if __name__ == '__main__':
    # FIXME: Add way to give the config values via argparse without messing the odysseys framework
    REACTORCONSOLE = ReactorState()
    # Since the framework does not provide callbacks for clean shutdowns we must use ataxit as last resort
    atexit.register(REACTORCONSOLE.cleanup)
    TASK_OPTIONS = {
        'callback': REACTORCONSOLE.framework_update,
        'init': REACTORCONSOLE.framework_init,
        'run_interval': 1.0 / UPDATE_FPS,
    }
    # This will run forever
    TaskBoxRunner(TASK_OPTIONS).run()
