#!/usr/bin/env python3
"""Local logic for the "reactor console" """
import asyncio
import atexit
import functools
import logging
import os
import sys
import time

import ardubus_core
import ardubus_core.deviceconfig
import ardubus_core.events
import ardubus_core.transport

# This is F-UGLY but can't be helped, the framework is not packaged properly
sys.path.append(os.path.realpath(os.path.join(os.path.dirname(__file__), '..')))  # isort:skip
from odysseus import log  # noqa: F401 ; isort:skip  ; # pylint: disable=C0413,W0611,E0401
from odysseus.taskbox import TaskBoxRunner  # isort:skip ; # pylint: disable=C0413,E0401

UPDATE_FPS = 15  # How often to call updates
FORCE_UPDATE_INTERVAL = 1.0  # How often to force-update all states to HW
GAUGE_TICK_SPEED = 1.0 / UPDATE_FPS / 10  # 10 seconds to run gauge from (normalized) end to end
GAUGE_MAX_HW_VALUE = 170


def log_exceptions(f, re_raise=True):
    """Decorator to log exceptions that are easy to lose in callbacks"""

    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logging.getLogger().exception(e)
            if re_raise:
                raise e
    return wrapped


class ReactorState:
    """Keep track of the state without using fugly global variables"""
    ardubus_devicename = 'rod_control_panel'
    serialpath = None
    devicesyml_path = None
    aliases = None
    ardubus = None
    ardubus_transport = None
    gauge_directions = None
    gauge_values = None
    topled_values = None
    colorled_values = None
    last_full_update = 0
    logger = None

    def __init__(self, serialpath='/dev/ttyUSB0', devicesyml_path='./ardubus_devices.yml', loglevel=logging.INFO):
        self._aioloop = asyncio.get_event_loop()
        self.serialpath = serialpath
        self.devicesyml_path = devicesyml_path
        self.aliases = {}
        self.ardubus = {}
        self.ardubus_transport = None
        self.gauge_directions = {}
        self.gauge_values = {}
        self.topled_values = {}
        self.colorled_values = [0.0 for _ in range(32)]
        self.last_full_update = 0

        # init standard logging
        ardubus_core.init_logging(loglevel)
        self.logger = logging.getLogger('reactorconsole')

        self._init_ardubus_transport()

    @log_exceptions
    def _init_ardubus_transport(self):
        """Initialize ardubus transport"""
        self.logger.info("Initializing ardubus")
        ardubus_core.deviceconfig.load_devices_yml(self.devicesyml_path)
        # Shortcuts to configs
        self.ardubus = ardubus_core.deviceconfig.FULL_CONFIG_MAP[self.ardubus_devicename]
        self.aliases = ardubus_core.deviceconfig.ALIAS_MAP[self.ardubus_devicename]
        # Init the serial transport
        self.ardubus_transport = ardubus_core.transport.get(self.serialpath, self.ardubus)
        # Register the callback
        self.ardubus_transport.events_callback = self.ardubus_callback

    @log_exceptions
    def _reset_console_values(self):
        """Reset all console values to default"""
        self.logger.debug('called')

        for alias in self.aliases:
            if alias.endswith('_gauge'):
                self.gauge_values[alias] = 0.0
                # Init the up/down switch states too
                up_alias = alias.replace('_gauge', '_up')
                dn_alias = alias.replace('_gauge', '_down')
                self.gauge_directions[up_alias] = False
                self.gauge_directions[dn_alias] = False
                self.logger.debug('Initialized aliases {}'.format((alias, up_alias, dn_alias)))
            if alias.endswith('_led'):
                self.topled_values[alias] = 0.0
                self.logger.debug('Initialized alias {}'.format(alias))

        # The colored led clusters don't have aliases (yet, also maybe not super useful either)
        self.colorled_values = [0.0 for _ in range(32)]
        self.logger.debug('Initialized {} colorled values'.format(len(self.colorled_values)))

        self.logger.debug('gauge_values: {}'.format(repr(self.gauge_values)))
        self.logger.debug('gauge_directions: {}'.format(repr(self.gauge_directions)))
        self.logger.debug('colorled_values: {}'.format(repr(self.colorled_values)))

        self._do_full_update()

    @log_exceptions
    def _update_colorled_value(self, ledidx):
        """Maps the normalized led value to the hw value and returns a coroutine that sends it"""
        send_value = round(self.colorled_values[ledidx] * 255)
        self.logger.debug('#{} send_value={} (normalized was {})'.format(ledidx, send_value, self.colorled_values[ledidx]))
        # These have no aliases, we know that the colorleds are on board 1
        return self.ardubus['pca9635RGBJBOL_maps'][1][ledidx]['PROXY'].set_value(send_value)

    @log_exceptions
    def _update_topled_value(self, alias):
        """Maps the normalized led value to the hw value and returns a coroutine that sends it"""
        send_value = round(self.topled_values[alias] * 255)
        self.logger.debug('{} send_value={} (normalized was {})'.format(alias, send_value, self.topled_values[alias]))
        # NOTE! This is a coroutine
        return self.aliases[alias]['PROXY'].set_value(send_value)

    @log_exceptions
    def _update_gauge_value(self, alias):
        """Maps the normalized gauge value to the hw value and returns a coroutine that sends it"""
        send_value = round(self.gauge_values[alias] * GAUGE_MAX_HW_VALUE)
        self.logger.debug('{} send_value={} (normalized was {})'.format(alias, send_value, self.gauge_values[alias]))
        # NOTE! This is a coroutine
        return self.aliases[alias]['PROXY'].set_value(send_value)

    @log_exceptions
    def _do_full_update(self):
        """Send all values to HW"""
        self.logger.debug('called')
        # Keep track of what we need to do
        run_coros = []
        # Add all aliased values to the queue
        for alias in self.aliases:
            if alias.endswith('_gauge'):
                run_coros.append(self._update_gauge_value(alias))
            if alias.endswith('_led'):
                run_coros.append(self._update_topled_value(alias))
        # Add the nonaliased leds to queue
        for idx, _ in enumerate(self.colorled_values):
            run_coros.append(self._update_colorled_value(idx))
        # Run all the jobs
        self.logger.info('About to process {} commands'.format(len(run_coros)))
        self._aioloop.run_until_complete(asyncio.gather(*run_coros))
        self.last_full_update = time.time()

    @log_exceptions
    def ardubus_callback(self, event):
        """Ardubus events callback"""
        if not isinstance(event, ardubus_core.events.Status):
            self.logger.debug('Called with {}'.format(event))
        if event.alias in self.gauge_directions:
            # active-low signalling, invert the value for nicer logic flow
            self.gauge_directions[event.alias] = not event.state
            return

        # TODO add handling for the commit switch

        self.logger.warning('Unhandled event {}'.format(event))

    @log_exceptions
    def framework_init(self):
        """Called by the odysseys framework on init"""
        self.logger.debug('called')
        self._reset_console_values()

    @log_exceptions
    def framework_update(self, state, backend_change):
        """Called by the odysseys framework periodically"""
        self.logger.debug('called')
        now = time.time()
        # Keep track of what we need to do
        run_coros = []
        # Check if we are going to do full update anyway
        full_update_pending = False
        if (now - self.last_full_update) > FORCE_UPDATE_INTERVAL:
            full_update_pending = True

        if backend_change:
            self.logger.debug("Changed state from backend: {}".format(repr(state)))

        # TODO: implement missing logic, remember to add tasks to run_coros only if full_update_pending is False

        # Move gauges
        for gauge_alias in self.gauge_values:
            up_alias = gauge_alias.replace('_gauge', '_up')
            dn_alias = gauge_alias.replace('_gauge', '_down')
            new_value = self.gauge_values[gauge_alias]
            if self.gauge_directions[up_alias]:
                self.logger.debug('Moving {} UP'.format(gauge_alias))
                new_value += GAUGE_TICK_SPEED
            if self.gauge_directions[dn_alias]:
                self.logger.debug('Moving {} DOWN'.format(gauge_alias))
                new_value -= GAUGE_TICK_SPEED
            if self.gauge_directions[dn_alias] and self.gauge_directions[up_alias]:
                self.logger.error('Aliases {} are both set, some swith is b0rked!'.format([up_alias, dn_alias]))
                # It's a no-op, no need to check this alias further
                continue
            # Limit the values
            if new_value < 0.0:
                self.logger.debug('{} limited to 0.0'.format(gauge_alias))
                new_value = 0.0
            if new_value > 1.0:
                self.logger.debug('{} limited to 1.0'.format(gauge_alias))
                new_value = 1.0
            if not full_update_pending and new_value != self.gauge_values[gauge_alias]:
                run_coros.append(run_coros.append(self._update_gauge_value(gauge_alias)))

        if full_update_pending:
            self._do_full_update()
        elif run_coros:
            self._aioloop.run_until_complete(asyncio.gather(*run_coros))

    @log_exceptions
    def cleanup(self):
        """cleanup on quit"""
        if not self.ardubus_transport:
            return
        self._reset_console_values()
        self._aioloop.run_until_complete(self.ardubus_transport.quit())


if __name__ == '__main__':
    # FIXME: Add way to give the config values via argparse without messing the odysseys framework
    REACTORCONSOLE = ReactorState()
    # Set debug only for our local logger
    REACTORCONSOLE.logger.setLevel(logging.DEBUG)
    # Since the framework does not provide callbacks for clean shutdowns we must use ataxit as last resort
    atexit.register(REACTORCONSOLE.cleanup)
    TASK_OPTIONS = {
        'callback': REACTORCONSOLE.framework_update,
        'init': REACTORCONSOLE.framework_init,
        'run_interval': 1.0 / UPDATE_FPS,
    }
    # This will run forever
    TaskBoxRunner(TASK_OPTIONS).run()
