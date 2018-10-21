import copy
import argparse
import json
from time import time, sleep
from pathlib import Path

class ConcurrentModificationException(Exception):
    pass


class TaskBox:
    def __init__(self, id):
        self.id = id
    def read(self):
        raise Exception('NOT YET IMPLEMENTED')
    def write(self, newState):
        raise Exception('NOT YET IMPLEMENTED')


class MockTaskBox:
    def __init__(self, id, initial_state):
        self.id = id
        self.state = initial_state
        self.mock_state_file = Path("backend-mock-" + id + ".json")
        print("Mock backend created, write to file '" + str(self.mock_state_file)
         + "' to change backend state")

    def read(self):
        new_state = self._read_and_delete()
        if new_state:
            print("READ NEW MOCK BACKEND STATE")
            self.state = new_state
        print("READ(" + self.id + "):  " + str(self.state))
        return self.state

    def write(self, newState):
        new_state = self._read_and_delete()
        if new_state:
            print("BACKEND STATE HAS CHANGED, CAUSING EXCEPTION")
            self.state = new_state
            raise ConcurrentModificationException
        self.state = newState
        print("WRITE(" + self.id + "): " + str(self.state))

    def _read_and_delete(self):
        if self.mock_state_file.is_file():
            with self.mock_state_file.open() as source:
                data = json.load(source)
            self.mock_state_file.unlink()
            return data
        return None



class TaskBoxRunner:
    def __init__(self, options):
        self._parse_command_line(options)
        self._defaults(options)
        self._validate(options)

        if options['mock']:
            self._box = MockTaskBox(options['id'], options.get('mock_init', {}))
        else:
            self._box = TaskBox(options['id'])

        self._callback = options['callback']
        self.options = options

    def run(self):
        run_interval = self.options['run_interval']
        poll_interval = self.options['poll_interval']
        write_interval = self.options['write_interval']

        self._previous_backend_state = None
        self._state = None
        self._state_changed = False

        next_run_time = time()
        next_poll_time = time()
        next_write_time = time()

        while True:
            self._wait_until(min(next_run_time, next_poll_time))

            if time() >= next_poll_time:
                self._poll_backend()
                next_poll_time = self._inc_time(next_poll_time, poll_interval)

            if time() >= next_run_time:
                self._call_callback(False)
                next_run_time = self._inc_time(next_run_time, run_interval)

            if self._state_changed and time() >= next_write_time:
                self._write_backend()
                next_write_time = time() + write_interval


    def _poll_backend(self):
        read_state = self._box.read()  # FIXME: Handle network errors
        if read_state != self._previous_backend_state:
            # State changed in backend server
            self._state = read_state
            self._previous_backend_state = read_state
            self._state_changed = False
            self._call_callback(True)


    def _write_backend(self):
        try:
            self._box.write(self._state)  # FIXME: Handle network errors
            self._previous_backend_state = self._state
            self._state_changed = False
        except ConcurrentModificationException:
            self._previous_backend_state = None
            self._poll_backend()


    def _call_callback(self, backend_change):
        new_state = self._callback(copy.deepcopy(self._state), backend_change)
        if new_state != None and new_state != self._state:
            self._state = new_state
            self._state_changed = True


    def _wait_until(self, t):
        while (t > time()):
            sleep(t - time())


    def _defaults(self, options):
        options['mock'] = options.get('mock', False)
        options['poll_interval'] = options.get('poll_interval', 10)
        options['write_interval'] = options.get('write_interval', 0)


    def _validate(self, options):
        if 'id' not in options:
            raise Exception("'id' is not defined, use --id <myid>")
        if 'callback' not in options:
            raise Exception("'callback' is not defined")
        if 'run_interval' not in options:
            raise Exception("'run_interval' is not defined")

    def _inc_time(self, t, increment):
        t = t + increment
        if t < time()-increment:
            t=time()
        return t

    def _parse_command_line(self, options):
        parser = argparse.ArgumentParser(description='Task box startup')
        parser.add_argument('--id', help='Task box ID in backend')
        parser.add_argument('--mock', action='store_true', help='Use mock backend')
        parser.add_argument('--mock-init', help='Initial JSON state of mock')
        args = parser.parse_args()
        if args.id:
            options["id"] = args.id
        if args.mock:
            options["mock"] = True
        if args.mock_init:
            options["mock_init"] = json.loads(args.mock_init)
