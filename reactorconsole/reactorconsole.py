#!/usr/bin/env python3
"""Local logic for the "reactor console" """
import asyncio
import atexit
import enum
import functools
import logging
import os
import random
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
GAUGE_TICK_SPEED = (1.0 / LOCAL_UPDATE_FPS) / 7.5  # 7.5 seconds to run gauge from (normalized) end to end
GAUGE_MAX_HW_VALUE = 180
GAUGE_LEEWAY = GAUGE_TICK_SPEED * 4  # by how much the guage value can be off the backend expected
ARMED_TOP_TEXT = '-----'
RED_LEDS_IDX = (
    4, 5, 6, 7,
    12, 13, 14, 15,
    20, 21, 22, 23,
    28, 29, 30, 31,
)
RED_LEDS_DIM = 0.1
COLORLED_DEFAULT_GLOBAL_DIM = 0.25
BLINKENLICHTEN_DEFAULT = True
JUMPING_GAUGE_DRIFT_SPEED = (1.0 / LOCAL_UPDATE_FPS) / 90  # 1.5 minutes to drift from full to down


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


class CommitState(enum.IntEnum):
    """Handle states of the commit switches"""
    unintialized = 0
    ready = 1
    armed = 2
    committed = 3
    send_commit = 4
    commit_sent = 5


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
    colorled_global_dimming = COLORLED_DEFAULT_GLOBAL_DIM
    backend_state_changed_flag = False
    commit_arm_state = CommitState.unintialized
    toptext = ''
    gauges_match_expected = False
    arm_previous_top_text = ''
    use_random_blinkenlichten = BLINKENLICHTEN_DEFAULT
    full_update_pending = False

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
        self._arm_blink_active = False

        # init standard logging
        ardubus_core.init_logging(loglevel)
        self.logger = logging.getLogger('reactorconsole')

        # Init local update thread
        self.logger.info('Starting local update thread')
        self.local_update_thread = threading.Thread(target=self._start_local_update_loop)
        self.local_update_thread.start()

    @log_exceptions
    def _local_update_loop_move_gauges(self, run_coros):
        """Handle the gauge update part"""
        with self.event_state_lock:
            # Move gauges
            for gauge_alias in self.gauge_values:
                up_alias = gauge_alias.replace('_gauge', '_up')
                dn_alias = gauge_alias.replace('_gauge', '_down')
                new_value = self.gauge_values[gauge_alias]

                if self.gauge_directions[dn_alias] and self.gauge_directions[up_alias]:
                    self.logger.error('Aliases {} are both set, some swith is b0rked!'.format([up_alias, dn_alias]))
                    # It's a no-op, no need to check this alias further
                    continue

                if self.gauge_directions[up_alias]:
                    if self.commit_arm_state >= CommitState.armed:
                        asyncio.get_event_loop().create_task(self._blink_armed_text())
                        self.logger.info('Trying to move {} but we are in armed stated {}'.format(
                            gauge_alias, self.commit_arm_state))
                    else:
                        self.logger.debug('Moving {} UP'.format(gauge_alias))
                        new_value = self.gauge_values[gauge_alias] + GAUGE_TICK_SPEED
                elif self.gauge_directions[dn_alias]:
                    if self.commit_arm_state >= CommitState.armed:
                        asyncio.get_event_loop().create_task(self._blink_armed_text())
                        self.logger.info('Trying to move {} but we are in armed stated {}'.format(
                            gauge_alias, self.commit_arm_state))
                    else:
                        self.logger.debug('Moving {} DOWN'.format(gauge_alias))
                        new_value = self.gauge_values[gauge_alias] - GAUGE_TICK_SPEED
                elif self.backend_state.get('jumping', False):
                    # If not actively controlled and jumping, slowly drift gauges down
                    new_value = self.gauge_values[gauge_alias] - JUMPING_GAUGE_DRIFT_SPEED

                # Limit the values
                if new_value < 0.0:
                    self.logger.debug('{} limited to 0.0 (was {})'.format(gauge_alias, new_value))
                    new_value = 0.0
                if new_value > 1.0:
                    self.logger.debug('{} limited to 1.0 (was {})'.format(gauge_alias, new_value))
                    new_value = 1.0
                # Schedule immediate update if needed
                if not self.full_update_pending and new_value != self.gauge_values[gauge_alias]:
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
        lower_bound = exp_value - GAUGE_LEEWAY
        # self.logger.debug('{} check {} < {} < {}'.format(gauge_alias, lower_bound,
        #                                                  self.gauge_values[gauge_alias], upper_bound))
        if lower_bound < self.gauge_values[gauge_alias] < upper_bound:
            return True
        return False

    @log_exceptions
    def _local_update_loop_check_gauges(self, run_coros):
        """Check backend expected vs current value and set the topleds accordingly"""
        self.gauges_match_expected = True
        if not self.backend_state:
            self.logger.warning('No backend state yet, aborting check')
            return run_coros
        if 'expected' not in self.backend_state:
            self.logger.error('Key "expected" not in backend state, aborting check')
            return run_coros

        # Set defined topleds to values according to expectation
        with self.backend_state_lock:
            # self.logger.debug('"expected" backend state: {}'.format(repr(self.backend_state['expected'])))
            # self.logger.debug('"lights" backend state: {}'.format(repr(self.backend_state['lights'])))
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
                    self.gauges_match_expected = False
                if not self.full_update_pending:
                    run_coros.append(self._update_topled_value(led_alias))

        # Make sure all other LEDs are off. (was a bad idea afterall)

        return run_coros

    @log_exceptions
    async def _invalid_commit_punish(self):  # pylint: disable=R0912
        """Punishment for invalid commit"""
        global RED_LEDS_DIM  # pylint: disable=W0603
        self.logger.info('PUNISH!!!')
        # Randomize gauge values
        run_commands = []
        for alias in self.gauge_values:
            backend_key = alias.replace('_gauge', '').replace('rod_', '')
            if random.random() > 0.5 or backend_key in self.backend_state['expected']:
                self.gauge_values[alias] = random.random()
                if not self.full_update_pending:
                    run_commands.append(self._update_gauge_value(alias))
        # Red LEDs pulse-effect
        blinker_backup = self.use_random_blinkenlichten
        red_fade_backup = RED_LEDS_DIM
        RED_LEDS_DIM = 1.0
        self.use_random_blinkenlichten = False
        # Turn reds on, greens off
        for idx, _ in enumerate(self.colorled_values):
            if idx in RED_LEDS_IDX:
                self.colorled_values[idx] = 1.0
            else:
                self.colorled_values[idx] = 0.0
            if not self.full_update_pending:
                run_commands.append(self._update_colorled_value(idx))
        if run_commands:
            asyncio.get_event_loop().create_task(self._handle_commands(run_commands))
        await asyncio.sleep(0.5)

        # Then fade reds out
        fade_steps = 25
        fade_time = 1.5
        for step in range(fade_steps):
            run_commands = []
            fade_value = 1.0 - (1.0 / fade_steps) * step
            for ledidx in RED_LEDS_IDX:
                self.colorled_values[ledidx] = fade_value
                if not self.full_update_pending:
                    run_commands.append(self._update_colorled_value(ledidx))
            if run_commands:
                asyncio.get_event_loop().create_task(self._handle_commands(run_commands))
            await asyncio.sleep(fade_time / fade_steps)

        # Keep them off for a moment
        await asyncio.sleep(1.5)

        # Restore previous blinker and dim state
        self.use_random_blinkenlichten = blinker_backup
        RED_LEDS_DIM = red_fade_backup

    @log_exceptions
    def _local_update_loop_arm_commit(self, run_coros):
        """Handle arm and commit"""
        with self.event_state_lock:
            if self.commit_arm_state == CommitState.ready:
                self.toptext = self.arm_previous_top_text
                if not self.full_update_pending:
                    run_coros.append(self._update_toptext())
            if self.commit_arm_state == CommitState.armed:
                self.arm_previous_top_text = self.toptext
                self.toptext = ARMED_TOP_TEXT
                if not self.full_update_pending:
                    run_coros.append(self._update_toptext())
            if self.commit_arm_state == CommitState.committed:
                if not self.gauges_match_expected:
                    asyncio.get_event_loop().create_task(self._invalid_commit_punish())
                else:
                    self.commit_arm_state = CommitState.send_commit
            if self.commit_arm_state == CommitState.commit_sent:
                self.toptext = self.arm_previous_top_text
                if not self.full_update_pending:
                    run_coros.append(self._update_toptext())

        return run_coros

    @log_exceptions
    def _local_update_loop_blinkenlighten(self, run_coros):
        """Blink the gauge LEDs randomly"""
        for idx, current_val in enumerate(self.colorled_values):
            if random.random() > 0.10:
                continue
            if current_val > 0:
                self.colorled_values[idx] = 0.0
            else:
                self.colorled_values[idx] = random.choice((0.25, 0.5, 1.0))
            if not self.full_update_pending:
                run_coros.append(self._update_colorled_value(idx))
        return run_coros

    @log_exceptions
    async def _enter_broken_effect(self):
        """Fade out the gauge LEDs when we enter broken state"""
        fade_steps = 50
        fade_time = 1.0
        dim_backup = self.colorled_global_dimming
        for step in range(fade_steps):
            run_commands = []
            fade_value = dim_backup - (dim_backup / fade_steps) * step
            self.colorled_global_dimming = fade_value
            for ledidx, _ in enumerate(self.colorled_values):
                if not self.full_update_pending:
                    run_commands.append(self._update_colorled_value(ledidx))
            if run_commands:
                asyncio.get_event_loop().create_task(self._handle_commands(run_commands))
            await asyncio.sleep(fade_time / fade_steps)
        # Set all LEDS off and restore global dimming
        run_commands = []
        for ledidx, _ in enumerate(self.colorled_values):
            self.colorled_values[ledidx] = 0.0
            if not self.full_update_pending:
                run_commands.append(self._update_colorled_value(ledidx))
        if run_commands:
            asyncio.get_event_loop().create_task(self._handle_commands(run_commands))
        self.colorled_global_dimming = dim_backup

    @log_exceptions
    def _local_update_loop_reset_topleds(self, run_coros):
        """Reset the topleds (called on backend state change"""
        for alias in self.topled_values:
            self.topled_values[alias] = 0.0
            if not self.full_update_pending:
                run_coros.append(self._update_topled_value(alias))
        return run_coros

    @log_exceptions
    def _local_update_loop_backend_toptext(self, run_coros):
        """Handle backend set toptext"""
        new_toptext = self.backend_state.get('toptext', None)
        if new_toptext is not None:
            self.arm_previous_top_text = new_toptext
            if self.toptext != ARMED_TOP_TEXT:
                self.toptext = new_toptext
                if not self.full_update_pending:
                    run_coros.append(self._update_toptext())
        return run_coros

    @log_exceptions
    async def _blink_armed_text(self):
        """Blink the armed text a few times"""
        # Guard against multiple blink tasks running at the same time
        if self._arm_blink_active:
            return
        self._arm_blink_active = True
        if self.toptext != ARMED_TOP_TEXT:
            self.arm_previous_top_text = self.toptext
        for idx in range(4):
            if idx % 2 == 0:
                self.toptext = ARMED_TOP_TEXT
            else:
                self.toptext = self.arm_previous_top_text
            if not self.full_update_pending:
                asyncio.get_event_loop().create_task(self._handle_commands([self._update_toptext()]))
            await asyncio.sleep(0.25)

        # Restore original value if needed
        if self.commit_arm_state == CommitState.armed and self.toptext != ARMED_TOP_TEXT:
            self.toptext = ARMED_TOP_TEXT
        elif self.toptext != self.arm_previous_top_text:
            self.toptext = self.arm_previous_top_text
        if not self.full_update_pending:
            asyncio.get_event_loop().create_task(self._handle_commands([self._update_toptext()]))
        self._arm_blink_active = False

    @log_exceptions
    async def _local_update_loop(self):
        """Coroutine that runs the main hw update loop"""
        await self._reset_console_values()
        self.logger.debug('Starting loop')
        handled_arm_state = None
        self.arm_previous_top_text = ''
        interval = (1.0 / LOCAL_UPDATE_FPS)
        while self.keep_running:
            # Keep track of what we need to do
            now = time.time()
            run_coros = []

            # Check if we are going to do full update anyway
            if (now - self.last_full_update) > FORCE_UPDATE_INTERVAL:
                self.full_update_pending = True

            # Reset stuff that needs reset when backend state changes
            if self.backend_state_changed_flag:
                self.backend_state_changed_flag = False
                # Stop random blink when we're broken (fixed state resets this in the framework update method)
                if self.backend_state.get('status', 'undef') == 'broken':
                    self.use_random_blinkenlichten = False
                    asyncio.get_event_loop().create_task(self._enter_broken_effect())
                # Top-leds
                run_coros = self._local_update_loop_reset_topleds(run_coros)
                # top-text
                run_coros = self._local_update_loop_backend_toptext(run_coros)

            # Other processing
            run_coros = self._local_update_loop_move_gauges(run_coros)
            run_coros = self._local_update_loop_check_gauges(run_coros)
            if self.use_random_blinkenlichten:
                run_coros = self._local_update_loop_blinkenlighten(run_coros)

            # Arming and committing
            if handled_arm_state != self.commit_arm_state:
                handled_arm_state = self.commit_arm_state
                run_coros = self._local_update_loop_arm_commit(run_coros)

            if self.full_update_pending:
                asyncio.get_event_loop().create_task(self._do_full_update())
            elif run_coros:
                asyncio.get_event_loop().create_task(self._handle_commands(run_coros))

            # sleep until it's time to do things again
            spent_time = time.time() - now
            if spent_time < interval:
                await asyncio.sleep(interval - spent_time)

    @log_exceptions
    def _start_local_update_loop(self):
        """Handle local interaction separate from the framework"""
        self.logger.debug('Called')
        # Initialize asyncio eventloop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Init serial transport
        self._init_ardubus_transport()
        self.logger.debug('Wait for arduino to finish initializing')
        time.sleep(2.0)
        # Add the local update as task and start the asyncioloop
        asyncio.get_event_loop().create_task(self._local_update_loop())
        loop.run_forever()

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
    async def _reset_console_values(self):
        """Reset all console values to default"""
        self.logger.debug('called')
        self.toptext = ''

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

        await self._do_full_update()

    @log_exceptions
    def _update_toptext(self):
        """Update the top-text"""
        # Right-align the text, we know we always have only 5 chars in the actual display
        send_value = '{:>5}'.format(self.toptext)
        self.logger.debug('Setting text to "{}"'.format(send_value))
        # NOTE! This is a coroutine
        return self.ardubus['i2cascii_boards'][0]['PROXY'].set_value(send_value)

    @log_exceptions
    def _update_colorled_value(self, ledidx):
        """Maps the normalized led value to the hw value and returns a coroutine that sends it"""
        dimmed = self.colorled_values[ledidx] * self.global_led_dimming_factor * self.colorled_global_dimming
        if ledidx in RED_LEDS_IDX:
            dimmed = dimmed * RED_LEDS_DIM
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
    async def _handle_commands(self, run_coros):
        """Send and time ardubus commands"""
        now = time.time()
        self.logger.debug('About to process {} commands'.format(len(run_coros)))
        # await asyncio.gather(*run_coros)
        # these all depend on same lock so maybe better to handle them sequentially and rate-limit
        rate_limit = len(run_coros) > 1
        for coro in run_coros:
            await coro
            if rate_limit:
                await asyncio.sleep(0.001)  # Rate limit the spam since we don't wait for responses
        diff = round((time.time() - now) * 1000)
        self.logger.debug('Commands done in {}ms'.format(diff))

    @log_exceptions
    async def _do_full_update(self):
        """Send all values to HW"""
        self.logger.debug('called')
        self.full_update_pending = False
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
        # Top-text/number
        run_coros.append(self._update_toptext())
        # Run all the jobs
        await self._handle_commands(run_coros)
        self.last_full_update = time.time()

    @log_exceptions
    def ardubus_callback(self, event):
        """Ardubus events callback"""
        with self.event_state_lock:
            if not isinstance(event, ardubus_core.events.Status):
                self.logger.debug('Called with {}'.format(event))

            if 'unused' in event.alias:
                return

            if event.alias in self.gauge_directions:
                # active-low signalling, invert the value for nicer logic flow
                self.gauge_directions[event.alias] = not event.state
                return

            if event.alias == 'commit_arm_key':
                # Active-low signalling, this is idle
                if event.state:
                    self.commit_arm_state = CommitState.ready
                # Arm only from idle
                if not event.state and self.commit_arm_state < CommitState.armed:
                    self.commit_arm_state = CommitState.armed
                return

            if event.alias == 'commit_push':
                # Active-HIGH sigalling
                if event.state and self.commit_arm_state == CommitState.armed:
                    self.commit_arm_state = CommitState.committed
                return

            self.logger.warning('Unhandled event {}'.format(event))

    @log_exceptions
    def framework_init(self):
        """Called by the odysseys framework on init"""
        self.logger.debug('called')

    @log_exceptions
    def framework_update(self, state, backend_change):
        """Called by the odysseys framework periodically"""
        with self.backend_state_lock:
            # self.logger.debug('called with state: {}'.format(repr(state)))
            if backend_change or (state and not self.backend_state):
                self.backend_state = state
                self.logger.debug("Changed state from backend: {}".format(repr(state)))
                self.backend_state_changed_flag = True

            # Set some basic state keys we expect to see elsewhere just to get rid of the warnings
            if self.backend_state is None:
                self.logger.warning('Setting hardcoded initial state since backend gave us None')
                self.backend_state = {
                    'expected': {'3_3': 0.5},
                    'lights': {'3_3': True},
                    'status': 'broken',
                }
                self.backend_state_changed_flag = True

            # Whether *we* changed the state
            state_changed = False
            if self.commit_arm_state == CommitState.send_commit:
                self.commit_arm_state = CommitState.commit_sent
                self.backend_state['status'] = 'fixed'
                self.use_random_blinkenlichten = BLINKENLICHTEN_DEFAULT
                state_changed = True

            if state_changed:
                return self.backend_state
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
    # REACTORCONSOLE.logger.setLevel(logging.DEBUG)
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
