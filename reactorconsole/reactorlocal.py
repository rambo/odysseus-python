#!/usr/bin/env python3
"""Local logic loop for the reactor console"""
import asyncio
import enum
import json
import logging
import random
import signal as posixsignal
import threading
import time

import ardubus_core
import ardubus_core.deviceconfig
import ardubus_core.transport
import zmq
import zmq.asyncio

from helpers import log_exceptions

LOCAL_UPDATE_FPS = 25  # How often the local logic loop does stuff
FORCE_UPDATE_INTERVAL = 30.0  # How often to force-update all states to HW
GAUGE_TICK_SPEED = round((1.0 / LOCAL_UPDATE_FPS) / 2, 2)  # 2 seconds to run gauge from (normalized) end to end
GAUGE_MAX_HW_VALUE = 180
GAUGE_LEEWAY = round(GAUGE_TICK_SPEED * 7, 2)  # by how much the gauge value can be off the backend expected
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
ALLOW_PUNISH = False
BROKEN_TOPLEDS = (
    '5_3',
    '3_0'
)


class CommitState(enum.IntEnum):
    """Handle states of the commit switches"""
    unintialized = 0
    ready = 1
    armed = 2
    committed = 3
    send_commit = 4
    commit_sent = 5


class ReactorConsole:
    """Local logic loop for the reactor console"""
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
    keep_running = True
    backend_state = None
    global_led_dimming_factor = 1.0
    colorled_global_dimming = COLORLED_DEFAULT_GLOBAL_DIM
    backend_state_changed_flag = False
    commit_arm_state = CommitState.unintialized
    toptext = ''
    gauges_match_expected = False
    arm_previous_top_text = ''
    use_random_blinkenlichten = BLINKENLICHTEN_DEFAULT
    full_update_pending = False
    last_topled_pattern_update = 0
    last_topled_status_print = 0
    already_broken = False

    def __init__(self, serialpath='/dev/ttyUSB0', devicesyml_path='./ardubus_devices.yml'):
        ardubus_core.init_logging(logging.INFO)
        self.logger = logging.getLogger('reactorconsole:locallogic')
        self.serialpath = serialpath
        self.devicesyml_path = devicesyml_path
        self.ardubus_transport = None

        self.keep_running = True
        self.loop = asyncio.get_event_loop()

        self.zmq_ctx = zmq.asyncio.Context()
        self.zmq_pub_socket = self.zmq_ctx.socket(zmq.PUB)  # pylint: disable=E1101
        self.zmq_sub_socket = self.zmq_ctx.socket(zmq.SUB)  # pylint: disable=E1101

        self.logger.debug('Binding/Connecting the ZMQ sockets')
        self.zmq_pub_socket.bind('ipc:///tmp/reactor_locallogic.zmq')
        self.zmq_sub_socket.connect('ipc:///tmp/reactor_backend.zmq')
        self.zmq_sub_socket.subscribe(b'backend2local')

        self.event_state_lock = threading.Lock()
        self.backend_state_lock = threading.Lock()
        self.backend_state_changed_flag = False
        self.backend_state = {}
        self.aliases = {}
        self.ardubus = {}
        self.gauge_directions = {}
        self.gauge_values = {}
        self.topled_values = {}
        self.colorled_values = [0.0 for _ in range(32)]
        self.last_full_update = 0
        self._arm_blink_active = False

    @log_exceptions
    async def _local_update_loop_move_gauges(self, run_coros):  # pylint: disable=R0912
        """Handle the gauge update part"""
        remaining_gauges = set(self.gauge_values.keys())
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
                elif self.backend_state and self.backend_state.get('jumping', False):
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
                    remaining_gauges.remove(gauge_alias)
                    run_coros.append(self._update_gauge_value(gauge_alias))
                # The actual hw update is executed later so this is fine.
                self.gauge_values[gauge_alias] = new_value

        # Update some extra gauges every iteration (to get eventually rid of glitched ones)
        if not self.full_update_pending and remaining_gauges:
            gauge_alias = random.choice(tuple(remaining_gauges))
            run_coros.append(self._update_gauge_value(gauge_alias))

        return run_coros

    @log_exceptions
    def _gauge_within_expected(self, position):
        """Check if gauge is close enough to exepected value"""
        gauge_alias = 'rod_{}_gauge'.format(position)
        if position in BROKEN_TOPLEDS:
            self.logger.warning('top-LED {} is broken, returning true'.format(position))
            return True

        if 'expected' not in self.backend_state:
            self.logger.error('Key "expected" not in backend state, aborting check')
            return True

        exp_value = self.backend_state['expected'][position]

        if gauge_alias not in self.gauge_values:
            self.logger.error('No gauge "{}" defined'.format(gauge_alias))
            return False

        upper_bound = round(exp_value + GAUGE_LEEWAY, 2)
        lower_bound = round(exp_value - GAUGE_LEEWAY, 2)
        # self.logger.debug('{} check {} < {} < {}'.format(gauge_alias, lower_bound,
        #                                                  self.gauge_values[gauge_alias], upper_bound))
        if lower_bound < round(self.gauge_values[gauge_alias], 2) < upper_bound:
            return True
        return False

    @log_exceptions
    async def _test_topleds(self):
        """Turn all top-leds on and off"""
        run_coros = []
        self.logger.info('Turning top-LED on')
        for led_alias in self.aliases:
            if not led_alias.endswith('_led'):
                continue
            self.topled_values[led_alias] = 1.0
            run_coros.append(self._update_topled_value(led_alias))
        await self._handle_commands(run_coros)
        await asyncio.sleep(2)
        run_coros = []
        self.logger.info('Turning top-LED off')
        for led_alias in self.aliases:
            if not led_alias.endswith('_led'):
                continue
            self.topled_values[led_alias] = 0.0
            run_coros.append(self._update_topled_value(led_alias))
        await self._handle_commands(run_coros)

    @log_exceptions
    async def _test_colorleds(self):
        run_coros = []
        self.logger.info('Turning color-LEDs on')
        for idx, _ in enumerate(self.colorled_values):
            self.colorled_values[idx] = 1.0
            run_coros.append(self._update_colorled_value(idx))
        await self._handle_commands(run_coros)
        await asyncio.sleep(2)
        run_coros = []
        self.logger.info('Turning color-LEDs off')
        for idx, _ in enumerate(self.colorled_values):
            self.colorled_values[idx] = 0.0
            run_coros.append(self._update_colorled_value(idx))
        await self._handle_commands(run_coros)

    @log_exceptions
    async def _local_update_loop_check_gauges(self, run_coros):  # pylint: disable=R0912
        """Check backend expected vs current value and set the topleds accordingly"""
        self.gauges_match_expected = True
        if not self.backend_state:
            # self.logger.warning('No backend state yet, aborting check')
            return run_coros

        report_led_status = False
        if time.time() - self.last_topled_status_print > 10:
            report_led_status = True
            self.last_topled_status_print = time.time()

        if report_led_status:
            self.logger.info('Status is "{}"'.format(self.backend_state.get('status', 'undef')))

        if self.backend_state.get('status', 'undef') != 'broken':
            self.logger.debug('Status is not "broken", skipping check')
            return run_coros
        if 'expected' not in self.backend_state:
            self.logger.error('Key "expected" not in backend state, aborting check')
            return run_coros

        force_led_update = False
        if time.time() - self.last_topled_pattern_update > 1:
            force_led_update = True
            self.last_topled_pattern_update = time.time()

        # Set defined topleds to values according to expectation
        leds_remaining = set(self.topled_values.keys())
        ok_positions = []
        update_toptext = False
        expected_count_adjust = 0
        with self.backend_state_lock:
            # self.logger.debug('"expected" backend state: {}'.format(repr(self.backend_state['expected'])))
            # self.logger.debug('"lights" backend state: {}'.format(repr(self.backend_state['lights'])))
            for position in sorted(self.backend_state['expected'].keys()):
                if position in BROKEN_TOPLEDS:
                    # Skip ones where we have no feedback to the user
                    expected_count_adjust -= 1
                    continue
                if position not in self.backend_state['lights']:
                    self.logger.error('No light state defined for expected position {}'.format(position))
                    continue
                led_value = 0.0
                if self.backend_state['lights'][position]:
                    led_value = 1.0
                led_alias = 'rod_{}_led'.format(position)
                gauge_alias = 'rod_{}_gauge'.format(position)

                # Safety against nonexisting leds/gauges
                if led_alias not in self.aliases:
                    # self.logger.error('No top-LED "{}", aborting check for this position'.format(led_alias))
                    continue
                if gauge_alias not in self.aliases:
                    # self.logger.error('No gauge "{}", aborting check for this position'.format(gauge_alias))
                    continue

                up_alias = gauge_alias.replace('_gauge', '_up')
                dn_alias = gauge_alias.replace('_gauge', '_down')

                if self._gauge_within_expected(position):
                    if report_led_status:
                        self.logger.info('position {} is OK, led_Value={}'.format(position, led_value))
                    self.topled_values[led_alias] = led_value
                    ok_positions.append(gauge_alias)
                else:
                    if report_led_status:
                        self.logger.info('position {} is NFG (expected={}, value={:.2f}), led_value={}'.format(
                            position, self.backend_state['expected'][position],
                            self.gauge_values[gauge_alias], led_value
                        ))
                    self.topled_values[led_alias] = 1.0 - led_value
                    self.gauges_match_expected = False

                if not force_led_update and not self.gauge_directions[up_alias] and not self.gauge_directions[dn_alias]:
                    # Skip led updates that have not been moved
                    continue
                update_toptext = True

                if not self.full_update_pending:
                    leds_remaining.remove(led_alias)
                    run_coros.append(self._update_topled_value(led_alias))

        if update_toptext and self.commit_arm_state < CommitState.armed:
            self.toptext = '{}-{}'.format(
                len(ok_positions), len(self.backend_state['expected']) + expected_count_adjust
            )
            if not self.full_update_pending:
                run_coros.append(self._update_toptext())

        if report_led_status:
            self.logger.info('OK Gauges: {}/{} (flag={})'.format(
                len(ok_positions), len(self.backend_state['expected']) + expected_count_adjust,
                self.gauges_match_expected
            ))

        # Update some extra leds every iteration (to get eventually rid of glitched ones)
        if not self.full_update_pending and leds_remaining:
            led_alias = random.choice(tuple(leds_remaining))
            run_coros.append(self._update_topled_value(led_alias))

        return run_coros

    @log_exceptions
    async def _invalid_commit_punish(self):  # pylint: disable=R0912,R0914
        """Punishment for invalid commit"""
        global RED_LEDS_DIM  # pylint: disable=W0603
        self.logger.info('Expected leeway is +/-{}'.format(GAUGE_LEEWAY))
        for position in self.backend_state['expected']:
            gauge_alias = 'rod_{}_gauge'.format(position)
            # Ignore invalid positions
            if gauge_alias not in self.gauge_values:
                continue
            if not self._gauge_within_expected(position):
                self.logger.info('{}(={:.2f}) not within expected(={:.2f})'.format(
                    gauge_alias, self.gauge_values[gauge_alias], self.backend_state['expected'][position]
                ))
        # Randomize gauge values
        run_commands = []
        # Disable punishment for for now.
        if ALLOW_PUNISH:
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

        # Turn them all off (the dim does not turn them fully off)
        run_commands = []
        for ledidx in RED_LEDS_IDX:
            self.colorled_values[ledidx] = 0.0
            if not self.full_update_pending:
                run_commands.append(self._update_colorled_value(ledidx))
        if run_commands:
            asyncio.get_event_loop().create_task(self._handle_commands(run_commands))

        # Keep them off for a moment
        await asyncio.sleep(1.5)

        # Restore previous blinker and dim state
        self.use_random_blinkenlichten = blinker_backup
        RED_LEDS_DIM = red_fade_backup

    @log_exceptions
    async def _local_update_loop_arm_commit(self, run_coros):
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
                    self.loop.create_task(self._zmq_send_fixed())
            if self.commit_arm_state == CommitState.commit_sent:
                self.toptext = self.arm_previous_top_text
                if not self.full_update_pending:
                    run_coros.append(self._update_toptext())

        return run_coros

    @log_exceptions
    async def _local_update_loop_blinkenlighten(self, run_coros):
        """Blink the gauge LEDs randomly"""
        for idx, current_val in enumerate(self.colorled_values):
            change_prob = 0.05
            if self.backend_state and self.backend_state.get('broken_jump', False):
                change_prob = 0.25
            if random.random() > change_prob:
                continue
            if current_val > 0:
                self.colorled_values[idx] = 0.0
            else:
                self.colorled_values[idx] = random.choice((0.25, 0.5, 1.0))
            if not self.full_update_pending:
                run_coros.append(self._update_colorled_value(idx))
        return run_coros

    @log_exceptions
    async def _enter_broken_jump_effect(self):
        """Effect to run when 'jump_broken' is true, for now just copied the blinkenlichten thing but for topleds"""
        interval = (1.0 / LOCAL_UPDATE_FPS)
        change_prob = 0.15
        while self.backend_state and self.backend_state.get('broken_jump', False):
            # Keep track of what we need to do
            now = time.time()
            run_coros = []

            for alias in self.topled_values:
                # Check for broken state so we won't mess with the actual fixing task by blinking the topleds
                with self.backend_state_lock:
                    if self.backend_state.get('status', 'undef') == 'broken':
                        continue
                    current_val = self.topled_values[alias]
                    if random.random() > change_prob:
                        continue
                    if current_val > 0:
                        self.topled_values[alias] = 0.0
                    else:
                        self.topled_values[alias] = random.choice((0.5, 1.0))
                    if not self.full_update_pending:
                        run_coros.append(self._update_topled_value(alias))

            if self.full_update_pending:
                asyncio.get_event_loop().create_task(self._do_full_update())
            elif run_coros:
                await self._handle_commands(run_coros)

            # sleep until it's time to do things again
            spent_time = time.time() - now
            if spent_time < interval:
                await asyncio.sleep(interval - spent_time)

    @log_exceptions
    async def _enter_broken_effect(self):
        """Fade out the gauge LEDs when we enter broken state"""
        if self.already_broken:
            return
        self.already_broken = True
        fade_steps = 15
        fade_time = 2.5
        dim_backup = self.colorled_global_dimming
        for step in range(fade_steps):
            step_started = time.time()
            run_commands = []
            fade_value = dim_backup - (dim_backup / fade_steps) * step
            self.colorled_global_dimming = fade_value
            for ledidx, _ in enumerate(self.colorled_values):
                if not self.full_update_pending:
                    run_commands.append(self._update_colorled_value(ledidx))
            if run_commands:
                await self._handle_commands(run_commands)
            step_done = time.time()
            took = step_done - step_started
            sleeptime = (fade_time / fade_steps) - took
            self.logger.debug('Step {}/{}'.format(step + 1, fade_steps))
            if sleeptime > 0:
                await asyncio.sleep(sleeptime)

        # Set all LEDS off and restore global dimming
        self.colorled_global_dimming = dim_backup
        run_commands = []
        self.logger.debug('Final turn-off command')
        for ledidx, _ in enumerate(self.colorled_values):
            self.colorled_values[ledidx] = 0.0
            if not self.full_update_pending:
                run_commands.append(self._update_colorled_value(ledidx))
        if run_commands:
            await self._handle_commands(run_commands)

    @log_exceptions
    async def _local_update_loop_reset_topleds(self, run_coros):
        """Reset the topleds (called on backend state change"""
        if not self.full_update_pending:
            run_coros.append(self.aliases['rod_3_3_led']['PROXY'].reset())
        return run_coros

    @log_exceptions
    async def _local_update_loop_backend_toptext(self, run_coros):
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
        self.logger.debug('Starting loop')
        handled_arm_state = None
        self.arm_previous_top_text = ''
        interval = (1.0 / LOCAL_UPDATE_FPS)
        await self.zmq_pub_socket.send_multipart([b'staterequest', b'1'])
        while self.keep_running:
            # Keep track of what we need to do
            now = time.time()
            run_coros = []

            # Check if we are going to do full update anyway
            if (now - self.last_full_update) > FORCE_UPDATE_INTERVAL:
                self.full_update_pending = True

            # Reset stuff that needs reset when backend state changes
            if self.backend_state_changed_flag and self.backend_state:
                self.backend_state_changed_flag = False
                # Stop random blink when we're broken (fixed state resets this in the framework update method)
                if self.backend_state.get('status', 'undef') == 'broken':
                    self.use_random_blinkenlichten = False
                    asyncio.get_event_loop().create_task(self._enter_broken_effect())
                if self.backend_state.get('broken_jump', False):
                    asyncio.get_event_loop().create_task(self._enter_broken_jump_effect())
                # Top-leds
                run_coros = await self._local_update_loop_reset_topleds(run_coros)
                # top-text
                run_coros = await self._local_update_loop_backend_toptext(run_coros)

            # Other processing
            run_coros = await self._local_update_loop_move_gauges(run_coros)
            run_coros = await self._local_update_loop_check_gauges(run_coros)
            if self.use_random_blinkenlichten:
                run_coros = await self._local_update_loop_blinkenlighten(run_coros)
            else:
                # update random led to eventually clear glitches
                # run_coros.append(self._update_colorled_value(random.randrange(len(self.colorled_values))))
                pass

            # Arming and committing
            if handled_arm_state != self.commit_arm_state:
                handled_arm_state = self.commit_arm_state
                run_coros = await self._local_update_loop_arm_commit(run_coros)

            if self.full_update_pending:
                await self._do_full_update()
            elif run_coros:
                await self._handle_commands(run_coros)

            # sleep until it's time to do things again
            spent_time = time.time() - now
            if spent_time < interval:
                await asyncio.sleep(interval - spent_time)

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

    async def _dummytask(self):
        """Does nothing, used to return coro from stuff that must always return one even if sanity checks fail"""

    @log_exceptions
    def _update_topled_value(self, alias):
        """Maps the normalized led value to the hw value and returns a coroutine that sends it"""
        if alias not in self.aliases or alias not in self.topled_values:
            self.logger.error('Invalid top-LED alias "{}"'.format(alias))
            return self._dummytask()

        dimmed = self.topled_values[alias] * self.global_led_dimming_factor
        send_value = round(dimmed * 255)
        self.logger.debug('{} send_value={} (normalized was {:0.3f})'.format(alias, send_value, dimmed))
        # NOTE! This is a coroutine
        return self.aliases[alias]['PROXY'].set_value(send_value)

    @log_exceptions
    def _update_gauge_value(self, alias):
        """Maps the normalized gauge value to the hw value and returns a coroutine that sends it"""
        if alias not in self.aliases or alias not in self.gauge_values:
            self.logger.error('Invalid gauge alias "{}"'.format(alias))
            return self._dummytask()

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
        # reset I2C LED controllers
        await self.aliases['rod_3_3_led']['PROXY'].reset()
        # Run all the jobs (in random order just in case some specific access pattern is more likely to glitch
        random.shuffle(run_coros)
        await self._handle_commands(run_coros)
        self.last_full_update = time.time()
        # Top-text is kinda important so update it twice
        await asyncio.sleep(0.1)
        await self._update_toptext()

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

    async def _zmq_send_fixed(self):
        """Set status to fixed and send it"""
        self.backend_state['status'] = 'fixed'
        self.toptext = self.backend_state['toptext']
        self.arm_previous_top_text = self.backend_state['toptext']
        self.loop.create_task(self._update_toptext())
        self.already_broken = False
        self.use_random_blinkenlichten = BLINKENLICHTEN_DEFAULT
        jsonstr = json.dumps(self.backend_state, ensure_ascii=False)
        await self.zmq_pub_socket.send_multipart([b'local2backend', jsonstr.encode('utf-8')])
        self.commit_arm_state = CommitState.commit_sent

    async def _zmq_receiver(self):
        """Handle incoming ZMQ messages"""
        while self.keep_running:
            msgparts = await self.zmq_sub_socket.recv_multipart()
            self.logger.info('Got ZMQ parts: {}'.format(msgparts))
            if msgparts[0] != b'backend2local':
                continue
            with self.backend_state_lock:
                new_state = json.loads(msgparts[1].decode('utf-8'))
                if new_state.get('status', None) is None:
                    new_state['status'] = 'undef'
                # PONDER: how to check actual change ?
                self.backend_state = new_state
                self.backend_state_changed_flag = True

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

    def run(self):
        """Start the eventloop"""
        # Register signal handlers
        self.loop.add_signal_handler(posixsignal.SIGTERM, self.quit)
        self.loop.add_signal_handler(posixsignal.SIGQUIT, self.quit)
        self.loop.add_signal_handler(posixsignal.SIGHUP, self.quit)
        self._init_ardubus_transport()
        time.sleep(2.0)
        # Reset everything
        self.loop.run_until_complete(self._reset_console_values())
        # Run some test blinks
        self.loop.run_until_complete(self._test_topleds())
        self.loop.run_until_complete(self._test_colorleds())

        # Add the local update as task and start the asyncioloop
        self.keep_running = True
        self.loop.create_task(self._zmq_receiver())
        self.loop.create_task(self._local_update_loop())

        self.loop.run_forever()

    def quit(self):
        """Stop the eventloop"""
        self.keep_running = False
        self.loop.stop()


if __name__ == '__main__':
    # FIXME: Add ports, paths and loglevel from cli
    REACTORCONSOLE = ReactorConsole()
    # Set debug only for our local logger
    # REACTORCONSOLE.logger.setLevel(logging.DEBUG)
    try:
        REACTORCONSOLE.run()
    except KeyboardInterrupt:
        REACTORCONSOLE.quit()
