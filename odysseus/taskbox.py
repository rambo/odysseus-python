import copy
import argparse
import json
from time import time, sleep
from pathlib import Path
import requests

_verbose = False

class ConcurrentModificationException(Exception):
    pass


class TaskBox:
    def __init__(self, id, url,initial_state,proxy,user,passwd):
        self.state = dict() 
        self.state['id'] = id
        self.url = url
        self.session = requests.Session()
        self.state['version']  = 0
        self.state['value'] = initial_state
        
        if proxy:
            self.session.proxies = {'http':proxy,'https':proxy}
            self.session.verify = False 
            if _verbose:
                print ("Using proxy: " + proxy)

        if user:
            self.session.auth=(user,passwd)
            if _verbose:
                print("Using basic http auth")

        self.session.headers['Content-Type']  = "application/json"

        if _verbose:
            print ("Session to server established: " + self.url)
        self.state['version'] = 1
        if _verbose:
            print ("Initial stage set to " + str(self.state))
    
        try:
            self.write(self.state) 
        except ConcurrentModificationException:
            self.state = self.read()



    def read(self):
        response=self.session.get( self.url + "/engineering/box/" + self.state['id'] )
        if response.status_code == requests.codes.ok:
            if int(response.headers['content-length']) > 4:
                response_json = json.loads(response.text)
                self.state = response_json

                if _verbose:
                        print ('Read from backend: ' + response.text + ' => ' + str(self.state))
                return self.state
            else:
                if _verbose:
                        print ("Backend state not defined, state not changed")
                return self.state
        else:
            raise Exception('State read from server failed ( ' + str(response.status_code) +')')
    
    def write(self, new_state):
            #payload = json.dumps({k:self.state[k] for k in ('id','version','value') if k in self.state})
            payload = json.dumps(self.state)
            response = self.session.post( self.url + "/engineering/box/" + self.state['id'], data=payload)

            if _verbose:
                print ("Write to backend: " + str(self.state) + " => " + response.text)

            if response.status_code == requests.codes['conflict']:
                raise ConcurrentModificationException

            if response.status_code != requests.codes.ok:
                 raise Exception('State write to server failed ( ' + str(response.status_code) +')')





class MockTaskBox:
    def __init__(self, id, initial_state):
        self.id = id
        self.state = initial_state
        self.mock_state_file = Path("backend-mock-" + id + ".json")
        print("Mock backend created, write to file '" + str(self.mock_state_file)
         + "' to change backend state")

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

        if options['mock_server']:
            self._box = MockTaskBox(options['id'], options.get('mock_initial_state', {}))
        else:
            self._box = TaskBox(options['id'], options['url'], options.get('initial_state',{}), options.get('proxy'), options.get('user'),options.get('passwd'))
            
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
        if read_state == {} and self.options.get("initial_state", None):
            # Set initial state
            self._box.write(self.options["initial_state"])
            read_state = self.options["initial_state"]
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
        new_state = self._callback(copy.deepcopy(self._state['value']), backend_change)
        if new_state != None and new_state != self._state['value']:
            self._state['value'] = new_state
            self._state['version'] = int(self._state['version']) + 1
            self._state_changed = True


    def _wait_until(self, t):
        while (t > time()):
            sleep(t - time())


    def _defaults(self, options):
        options['mock_pi'] = options.get('mock_pi', False)
        options['mock_server'] = options.get('mock_server', False)
        options['poll_interval'] = options.get('poll_interval', 10)
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
        parser.add_argument('--mock-init', help='Initial JSON state of mock')
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
        if args.mock_init:
            options['mock_initial_state'] = json.loads(args.mock_init)
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
                    
        