#!/usr/bin/env python3
"""Local logic for the "reactor console" """
import asyncio
import atexit
import functools
import logging
import os
import sys
import threading
import time

import ardubus_core
import ardubus_core.deviceconfig
import ardubus_core.events
import ardubus_core.transport

# This is F-UGLY but can't be helped, the framework is not packaged properly
sys.path.append(os.path.realpath(os.path.join(os.path.dirname(__file__), '..')))  # isort:skip
from odysseus import log  # noqa: F401 ; isort:skip  ; # pylint: disable=C0413,W0611,E0401
from odysseus.taskbox import TaskBoxRunner  # isort:skip ; # pylint: disable=C0413,E0401

FRAMEWORK_UPDATE_FPS = 15  # How often to call updates
LOCAL_UPDATE_FPS = 25  # How often the local logic loop does stuff
FORCE_UPDATE_INTERVAL = 10.0  # How often to force-update all states to HW
GAUGE_TICK_SPEED = (1.0 / LOCAL_UPDATE_FPS) / 10  # 10 seconds to run gauge from (normalized) end to end
GAUGE_MAX_HW_VALUE = 180
GAUGE_LEEWAY = GAUGE_TICK_SPEED * 4  # by how much the guage value can be off the backend expected


def log_exceptions(func, re_raise=True):
    """Decorator to log exceptions that are easy to lose in callbacks"""

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # pylint: disable=W0703
            logging.getLogger().exception(exc)
            if re_raise:
                raise exc
    return wrapped


class ReactorState:  # pylint: disable=R0902
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
    local_update_thread = None
    keep_running = True
    backend_state = None
    event_state_lock = threading.Lock()
    backend_state_lock = threading.Lock()
    global_led_dimming_factor = 1.0

    def __init__(self, serialpath='/dev/ttyUSB0', devicesyml_path='./ardubus_devices.yml', loglevel=logging.INFO):
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

        # Init local update thread
        self.logger.info('Starting local update thread')
        self.local_update_thread = threading.Thread(target=self._local_update_loop)
        self.local_update_thread.start()

    @log_exceptions
    def _local_update_loop_move_gauges(self, run_coros, full_update_pending):
        """Handle the gauge update part"""
        with self.event_state_lock:
            # Move gauges
            for gauge_alias in self.gauge_values:
                up_alias = gauge_alias.replace('_gauge', '_up')
                dn_alias = gauge_alias.replace('_gauge', '_down')
                new_value = self.gauge_values[gauge_alias]
                if self.gauge_directions[up_alias]:
                    self.logger.debug('Moving {} UP'.format(gauge_alias))
                    new_value = self.gauge_values[gauge_alias] + GAUGE_TICK_SPEED
                if self.gauge_directions[dn_alias]:
                    self.logger.debug('Moving {} DOWN'.format(gauge_alias))
                    new_value = self.gauge_values[gauge_alias] - GAUGE_TICK_SPEED
                if self.gauge_directions[dn_alias] and self.gauge_directions[up_alias]:
                    self.logger.error('Aliases {} are both set, some swith is b0rked!'.format([up_alias, dn_alias]))
                    # It's a no-op, no need to check this alias further
                    continue
                # Limit the values
                if new_value < 0.0:
                    self.logger.debug('{} limited to 0.0 (was {})'.format(gauge_alias, new_value))
                    new_value = 0.0
                if new_value > 1.0:
                    self.logger.debug('{} limited to 1.0 (was {})'.format(gauge_alias, new_value))
                    new_value = 1.0
                if not full_update_pending and new_value != self.gauge_values[gauge_alias]:
                    run_coros.append(self._update_gauge_value(gauge_alias))
                # The actual hw update is executed later so this is fine.
                self.gauge_values[gauge_alias] = new_value
        return run_coros

    @log_exceptions
    def _gauge_within_expected(self, position, exp_value):
        """Check if gauge is close enough to exepected value"""
        gauge_alias = 'rod_{}_gauge'.format(position)

        if gauge_alias not in self.gauge_values:
            self.logger.error('No gauge {} defined'.format(gauge_alias))
            return False

        upper_bound = exp_value + GAUGE_LEEWAY
        lower_bound = exp_value + GAUGE_LEEWAY
        if lower_bound < self.gauge_values[gauge_alias] < upper_bound:
            return True
        return False

    @log_exceptions
    def _local_update_loop_check_gauges(self, run_coros, full_update_pending):
        """Check backend expected vs current value and set the topleds accordingly"""
        if not self.backend_state:
            self.logger.warning('No backend state yet, aborting check')
            return run_coros
        if 'expected' not in self.backend_state:
            self.logger.error('Key "expected" not in backend state, aborting check')
            return run_coros

        # Set defined topleds to values according to expectation
        updated_leds = []
        with self.backend_state_lock:
            self.logger.debug('"expected" backend state: {}'.format(repr(self.backend_state['expected'])))
            self.logger.debug('"lights" backend state: {}'.format(repr(self.backend_state['lights'])))
            for position in self.backend_state['expected']:
                if position not in self.backend_state['lights']:
                    self.logger.error('No light state defined for expected position {}'.format(position))
                    continue
                exp_value = self.backend_state['expected'][position]
                led_value = float(self.backend_state['lights'][position])
                led_alias = 'rod_{}_led'.format(position)
                if self._gauge_within_expected(position, exp_value):
                    self.topled_values[led_alias] = led_value
                else:
                    self.topled_values[led_alias] = 1.0 - led_value
                if not full_update_pending:
                    run_coros.append(self._update_topled_value(led_alias))
                updated_leds.append(led_alias)

        # Turn all other topleds off
        for alias in self.topled_values:
            if alias in updated_leds:
                continue
            self.topled_values[alias] = 0.0
            if not full_update_pending:
                run_coros.append(self._update_topled_value(alias))

        return run_coros

    @log_exceptions
    def _local_update_loop(self):
        """Handle local interaction separate from the framework"""
        self.logger.debug('Called')
        # Initialize asyncio eventloop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Init serial transport
        self._init_ardubus_transport()
        self.logger.debug('Wait for arduino to finish initializing')
        time.sleep(2.0)
        self._reset_console_values()
        last_iteration = 0
        self.logger.debug('Starting loop')
        while self.keep_running:
            now = time.time()
            # wait for next iteration while yielding CPU & GIL
            if (now - last_iteration) < (1.0 / LOCAL_UPDATE_FPS):
                time.sleep(0)
                continue
            last_iteration = now

            # Keep track of what we need to do
            run_coros = []
            # Check if we are going to do full update anyway
            full_update_pending = False
            if (now - self.last_full_update) > FORCE_UPDATE_INTERVAL:
                full_update_pending = True

            run_coros = self._local_update_loop_move_gauges(run_coros, full_update_pending)
            run_coros = self._local_update_loop_check_gauges(run_coros, full_update_pending)

            # TODO: implement missing logic, remember to add tasks to run_coros only if full_update_pending is False

            if full_update_pending:
                self._do_full_update()
            elif run_coros:
                self._handle_commands(run_coros)

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
        # Just spam commands without waiting for responses
        self.ardubus_transport.command_wait_response = False

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
        dimmed = self.colorled_values[ledidx] * self.global_led_dimming_factor
        send_value = round(dimmed * 255)
        self.logger.debug('#{} send_value={} (normalized was {:0.3f})'.format(ledidx, send_value, dimmed))
        # These have no aliases, we know that the colorleds are on board 1
        return self.ardubus['pca9635RGBJBOL_maps'][1][ledidx]['PROXY'].set_value(send_value)

    @log_exceptions
    def _update_topled_value(self, alias):
        """Maps the normalized led value to the hw value and returns a coroutine that sends it"""
        dimmed = self.topled_values[alias] * self.global_led_dimming_factor
        send_value = round(dimmed * 255)
        self.logger.debug('{} send_value={} (normalized was {:0.3f})'.format(alias, send_value, dimmed))
        # NOTE! This is a coroutine
        return self.aliases[alias]['PROXY'].set_value(send_value)

    @log_exceptions
    def _update_gauge_value(self, alias):
        """Maps the normalized gauge value to the hw value and returns a coroutine that sends it"""
        send_value = round(self.gauge_values[alias] * GAUGE_MAX_HW_VALUE)
        self.logger.debug('{} send_value={} (normalized was {:0.3f})'.format(alias, send_value,
                                                                             self.gauge_values[alias]))
        # NOTE! This is a coroutine
        return self.aliases[alias]['PROXY'].set_value(send_value)

    @log_exceptions
    def _handle_commands(self, run_coros):
        """Send and time ardubus commands"""
        now = time.time()
        self.logger.info('About to process {} commands'.format(len(run_coros)))
        # asyncio.get_event_loop().run_until_complete(asyncio.gather(*run_coros))
        # these all depend on same lock so maybe better to handle them sequentially
        for coro in run_coros:
            asyncio.get_event_loop().run_until_complete(coro)
            time.sleep(0.002)  # Rate limit the spam since we don't wait for responses
        diff = round((time.time() - now) * 1000)
        self.logger.info('Commands done in {}ms'.format(diff))

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
        self._handle_commands(run_coros)
        self.last_full_update = time.time()

    @log_exceptions
    def ardubus_callback(self, event):
        """Ardubus events callback"""
        with self.event_state_lock:
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

    @log_exceptions
    def framework_update(self, state, backend_change):
        """Called by the odysseys framework periodically"""
        # self.logger.debug('called')
        with self.backend_state_lock:
            if backend_change or (state and not self.backend_state):
                self.backend_state = state
                self.logger.debug("Changed state from backend: {}".format(repr(state)))

            # Set some basic state keys we expect to see elsewhere just to get rid of the warnings
            if self.backend_state is None:
                self.logger.warning('Setting hardcoded initial state since backend gave us None')
                self.backend_state = {
                    'expected': {},
                    'lights': {},
                }

            # Whether *we* changed the state
            state_changed = False

            # TODO: Check local vs expected state, return new state if we changed something

            if state_changed:
                return state
            return None

    @log_exceptions
    def cleanup(self):
        """cleanup on quit"""
        if self.ardubus_transport:
            self._reset_console_values()
            asyncio.get_event_loop().run_until_complete(self.ardubus_transport.quit())
            self.ardubus_transport = None
        self.keep_running = False
        self.local_update_thread.join(5)


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
        'run_interval': 1.0 / FRAMEWORK_UPDATE_FPS,
    }
    # Catch ctrl-c and do a clean shutdown (if the odysseys framework ever lets us, right now they override
    # global exception handlers.
    try:
        # This will run forever
        TaskBoxRunner(TASK_OPTIONS).run()
    except KeyboardInterrupt:
        REACTORCONSOLE.cleanup()
        # If/when taskboxrunner is going to have clean shutdowns, add it here
        sys.exit(0)
