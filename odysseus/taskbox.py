import copy
import argparse
import json
from time import time, sleep
from pathlib import Path
import requests
import threading
import socketio


# Implementation notes:
#
# - Uses Socket.io to receive immediate notification of changes on server-side
#   - Does not use the received state as-is, instead immediately polls the server state.
#     This is to avoid using stale backend state in race conditions where many changes
#     are done quickly.
#   - Received data is checked to have corresponding ID and version number greater than
#     that received on last backend poll.
# - Polls backend regularly (~60s) in case Socket.io for some reason doesn't work
# - Write frequency can be throttled with `write_interval`
# - If backend cannot be reached on startup, will cause script to fail.
# - If backend connection is later lost, will continue using local state indefinitely.


_verbose = False

class ConcurrentModificationException(Exception):
    pass

class NetworkException(Exception):
    pass


# Server connector for reading / writing a taskbox
class TaskBox:
    def __init__(self, id, url, initial_state_value, proxy, user, passwd):
        self.id = id
        self.url = url
        self._setup_connection(proxy, user, passwd)
        self._setup_socketio()
        if _verbose:
            print("Session to server established: " + self.url)

    def _setup_connection(self, proxy, user, passwd):
        self.session = requests.Session()
        
        if proxy:
            self.session.proxies = {'http':proxy,'https':proxy}
            self.session.verify = False 
            if _verbose:
                print ("Using proxy: " + proxy)

        if user:
            self.session.auth = (user, passwd)
            if _verbose:
                print("Using basic http auth")

        self.session.headers['Content-Type']  = "application/json"


    def _setup_socketio(self):
        self.backend_event = threading.Event()
        self.received_state = None
        self.sio = socketio.Client()
        self.sio.connect(self.url + '?data=/data/box/' + self.id, namespaces=['/data'])

        @self.sio.on('dataUpdate', namespace='/data')
        def on_message(type, id, data):
            if _verbose:
                print('dataUpdate event: ' + str(data))
            self.received_state = data
            self.backend_event.set()


    def sleep(self, seconds):
        """Wait for the provided number of seconds, returning immediately
        if backend state changes. Returns the new state or None on timeout."""
        if self.backend_event.wait(timeout=seconds):
            state = self.received_state
            self.received_state = None
            self.backend_event.clear()
            return state
        return None


    def read(self):
        """Read state from server and return it. May raise NetworkException."""
        try:
            response = self.session.get(self.url + "/data/box/" + self.id)
        except Exception as e:
            raise NetworkException('State read from server failed (' + str(e) +')')
        if response.status_code == 200:
            state = json.loads(response.text)
            if _verbose:
                    print ('Read from backend: ' + str(state))
            return state
        else:
            raise NetworkException('State read from server failed (' + str(response.status_code) +')')
    
    def write(self, state):
        """Write state to server and return the new state. May raise NetworkException or ConcurrentModificationException."""
        payload = json.dumps(state)
        try:
            response = self.session.post(self.url + "/data/box/" + self.id, data=payload)
        except Exception as e:
            raise NetworkException('State write to server failed (' + str(e) +')')

        if _verbose:
            print ("Write to backend:\n    " + str(state) + "\n  =>\n    " + response.text)

        if response.status_code == requests.codes.conflict:
            raise ConcurrentModificationException

        if response.status_code != requests.codes.ok:
                raise NetworkException('State write to server failed (' + str(response.status_code) +')')

        state = json.loads(response.text)
        return state




# Mock implementation of server taskbox
class MockTaskBox:
    def __init__(self, id, initial_state_value):
        self.id = id
        self.state = initial_state_value
        self.mock_state_file = Path("backend-mock-" + id + ".json")
        print("Mock backend created, write to file '" + str(self.mock_state_file)
         + "' to change backend state")

    def sleep(self, seconds):
        sleep(seconds)
        return None

    def read(self):
        new_backend_state = self._read_and_delete()
        if new_backend_state:
            if _verbose:
                print("READ NEW MOCK BACKEND STATE")
            self.state = new_backend_state
        if _verbose:
            print("READ(" + self.id + "):  " + str(self.state))
        return self.state

    def write(self, new_state):
        new_backend_state = self._read_and_delete()
        if new_backend_state:
            if _verbose:
                print("BACKEND STATE HAS CHANGED, CAUSING EXCEPTION")
            self.state = new_backend_state
            raise ConcurrentModificationException
        self.state = new_state
        if _verbose:
            print("WRITE(" + self.id + "): " + str(self.state))
        return self.state

    def _read_and_delete(self):
        if self.mock_state_file.is_file():
            with self.mock_state_file.open() as source:
                data = json.load(source)
            self.mock_state_file.unlink()
            return data
        return None


# Taskbox logic runner
#
# Usage:
#
# options = {
#     "init": init,                     # init method
#     "init_mock": init_mock,           # init method when using mock raspi
#     "callback": logic,                # callback method
#     "run_interval": secs,             # run logic this often
#     "write_interval": 10,             # write every 10 secs
#     "initial_state": default_state,   # default state if box not defined
# }
# TaskBoxRunner(options).run()
class TaskBoxRunner:
    def __init__(self, options):
        self._parse_command_line(options)
        self._defaults(options)
        self._validate(options)

        self._mock = options['mock_server']
        if options['mock_server']:
            self._box = MockTaskBox(options['id'], options.get('initial_state', {}))
        else:
            self._box = TaskBox(options['id'], options['url'], options.get('initial_state', {}), options.get('proxy'), options.get('user'),options.get('passwd'))
        
        if options['mock_pi']:
            if options['init_mock']:
                options['init_mock']()
        else:
            if options['init']:
                options['init']()

        self._callback = options['callback']
        self.options = options

    def run(self):
        run_interval = self.options['run_interval']
        poll_interval = self.options['poll_interval']
        write_interval = self.options['write_interval']

        self._previous_backend_state = {}
        self._state = None
        self._state_changed = False

        next_run_time = time()
        next_poll_time = time()
        next_write_time = time()

        while True:
            if self._wait_until(min(next_run_time, next_poll_time)):
                # This does a poll instead of using the data sent from socket.io to avoid
                # using stale backend state in case many changes are done quickly.
                next_poll_time = time()
                if _verbose:
                    print("Detected backend state change, will poll backend next")

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
        try:
            read_state = self._box.read()
            if read_state == {} and self.options.get("initial_state", None):
                # Set initial state
                read_state = self._box.write(self.options["initial_state"])
            if read_state != self._previous_backend_state:
                # State changed in backend server
                if _verbose:
                    print("State changed in backend:\n    " + str(self._state) + "\n  =>\n    " + str(read_state))
                self._state = read_state
                self._previous_backend_state = read_state
                self._state_changed = False
                self._call_callback(True)
        except NetworkException as err:
            if self._state:
                print("NETWORK ERROR: Could not read from backend: " + str(err))
            else:
                raise Exception("Could not read/write initial backend state: " + str(err))


    def _write_backend(self):
        try:
            self._state = self._box.write(self._state)
            self._previous_backend_state = self._state
            self._state_changed = False
        except ConcurrentModificationException:
            self._previous_backend_state = {}
            self._poll_backend()
        except NetworkException as err:
            print("NETWORK ERROR: Could not write to backend: " + str(err))


    def _call_callback(self, backend_change):
        new_state = self._callback(copy.deepcopy(self._state), backend_change)
        if new_state != None and new_state != self._state:
            self._state = new_state
            self._state_changed = True


    # Returns True if detected backend state change during sleep
    def _wait_until(self, t):
        while (t > time()):
            state = self._box.sleep(t - time())
            # Do some sanity checks on state for safety
            if state and state.get('type', '') == 'box' and state.get('id', '') == self.options['id'] and state.get('version', 0) > self._previous_backend_state.get('version', 0):
                return True
        return False


    def _defaults(self, options):
        options['mock_pi'] = options.get('mock_pi', False)
        options['mock_server'] = options.get('mock_server', False)
        options['poll_interval'] = options.get('poll_interval', 60)
        options['write_interval'] = options.get('write_interval', 0)
        options['init'] = options.get('init', None)
        options['init_mock'] = options.get('init_mock', None)


    def _validate(self, options):
        if 'id' not in options:
            raise Exception("'id' is not defined, use --id <myid>")
        if 'callback' not in options:
            raise Exception("'callback' is not defined")
        if 'run_interval' not in options:
            raise Exception("'run_interval' is not defined")
        if options.get('url', '').endswith('/'):
            options['url'] = options['url'][:-1]

    def _inc_time(self, t, increment):
        t = t + increment
        if t < time()-increment:
            t=time()
        return t

    def _parse_command_line(self, options):
        global _verbose
        
        parser = argparse.ArgumentParser(description='Task box startup')
        parser.add_argument('--id', help='Task box ID in backend')
        parser.add_argument('--mock-pi', action='store_true', help='Use mock implementation instead of GPIO')
        parser.add_argument('--mock-server', action='store_true', help='Use mock backend')
        parser.add_argument('--run-interval', type=float, help='Override running interval (secs, float)')
        parser.add_argument('--poll-interval', type=float, help='Override polling interval (secs, float)')
        parser.add_argument('--write-interval', type=float, help='Override writing interval (secs, float)')
        parser.add_argument('--url',  help='Define target serve base URL in format <protocol>://<ipaddress>:<port>')
        parser.add_argument('--verbose', action='store_true', help='Print verbose messages during operation')
        parser.add_argument('--proxy', help='Use proxy for connections')
        parser.add_argument('--user', help='Username for basic http auth')
        parser.add_argument('--passwd', help='Password for basic http auth')

        args = parser.parse_args()
        if args.id:
            options['id'] = args.id
        if args.mock_pi:
            options['mock_pi'] = True
        if args.mock_server:
            options['mock_server'] = True
        if args.run_interval:
            options['run_interval'] = args.run_interval
        if args.poll_interval:
            options['poll_interval'] = args.poll_interval
        if args.write_interval:
            options['write_interval'] = args.write_interval
        if args.url:
            options['url'] = args.url
        if args.verbose:
            _verbose = True
        if args.proxy:
            options['proxy'] = args.proxy
        if args.user:
            if not args.passwd:
                print('Password not defined for basic http authentication')
                exit()
            options['user'] = args.user
            options['passwd'] = args.passwd
        if args.passwd:
            if not args.user:
                print('Username not defined for basic http authentication')
                exit()
                    
        