import copy
import argparse
import json
import requests
from time import time, sleep
from pathlib import Path

class ConcurrentModificationException(Exception):
    pass


class TaskBox:
    def __init__(self, id, url,initial_state,debug,proxy):
        self.id = id
        self.url = url
        self.debug = debug
        self.session = requests.Session()
        self.version = 0 
        
        if proxy:
            self.session.proxies = {'http':proxy}
            if self.debug:
                print ("Using proxy: " + proxy)
        
        self.session.headers['Content-Type']  = "application/json"

        if self.debug:
            print ("Session to server established: " + self.url)
        self.state = initial_state
        self.version = 1 
        if self.debug:
            print ("Initial stage set to " + str(self.state) + ", version: " + str(self.version))
        self.debug = debug
    
        self.write(self.state)


    def read(self):
        response=self.session.get( self.url + "/engineering/box/" + self.id)
        if response.status_code == requests.codes.ok:
            if int(response.headers['content-length']) > 4:
                response_json = json.loads(response.text)
                

                if str(response_json['value']) != '{"value":null}':
                    self.state = response_json['value']
                    self.version = response_json['version']
                else:
                    print ("Backend value json empty ( " + str(response_json['value']) + " )")
                
                if self.debug:
                        print ("Read from backend: " + response.text + " => " + str(self.state) + ", Version: " + self.version                                )
                return self.state
            else:
                if self.debug:
                        print ("Backend state not defined, state not changed")
                return self.state
        
        else:
            raise Exception('State read from server failed ( ' + response.status_code +')')
    
    def write(self, new_state):
        be_state = None
        be_version = None

        if new_state is not self.state:
            self.version = int(self.version) + 1 
            self.state = new_state

        response=self.session.get( self.url + "/engineering/box/" + self.id)
        if response.status_code == requests.codes.ok:
            if int(response.headers['content-length']) > 4:
                response_json = json.loads(response.text)
                if response_json['value'] != '{"value":null}':
                    be_state = response_json['value']
                    be_version = int(response_json['version'])
                    if self.debug:
                        print ("Read from backend: " + response.text)
            else:
                    self.state = new_state
                    print ("Backend state not defined, using " + str(new_state))
        else:
            raise Exception('State read from server failed ( ' + response.status_code +')')

        if be_version is None:
            be_version = 0 

        if self.version < be_version:
            self.state = be_state
            if self.debug:
                print ("Backend state changed, reverting to backend state. Version mismatch: " + str(be_version) + "(be) vs." +  str(self.version) + "(box)")
                raise ConcurrentModificationException
            

        if self.version > be_version:
            payload = '{ "id":"' + str(self.id) + '", "value":' + str(self.state).replace("'","\"") + ', "version":"' + str(self.version) + '"}'

            if self.debug:
                print ("Payload:" + payload)
            
            response = self.session.post( self.url + "/engineering/box/" + self.id, data=payload)

            if self.debug:
                print ("Write to backend: " + payload + " => " + response.text)

            if response.status_code != requests.codes.ok:
                 raise Exception('State write to server failed ( ' + str(response.status_code) +')')





class MockTaskBox:
    def __init__(self, id, initial_state, debug):
        self.id = id
        self.state = initial_state
        self.debug = debug
        self.mock_state_file = Path("backend-mock-" + id + ".json")
        print("Mock backend created, write to file '" + str(self.mock_state_file)
         + "' to change backend state")

    def read(self):
        new_backend_state = self._read_and_delete()
        if new_backend_state:
            if self.debug:
                print("READ NEW MOCK BACKEND STATE")
            self.state = new_backend_state
        if self.debug:
            print("READ(" + self.id + "):  " + str(self.state))
        return self.state

    def write(self, new_state):
        new_backend_state = self._read_and_delete()
        if new_backend_state:
            if self.debug:
                print("BACKEND STATE HAS CHANGED, CAUSING EXCEPTION")
            self.state = new_backend_state
            raise ConcurrentModificationException
        self.state = new_state
        if self.debug:
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
            self._box = MockTaskBox(options['id'], options.get('mock_initial_state', {}), options.get('mock_print', False))
        else:
            self._box = TaskBox(options['id'],options['url'],options.get('initial_state',{}),options.get('print', False), options.get('proxy'))
            
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
        new_state = self._callback(copy.deepcopy(self._state), backend_change)
        if new_state != None and new_state != self._state:
            self._state = new_state
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
        parser = argparse.ArgumentParser(description='Task box startup')
        parser.add_argument('--id', help='Task box ID in backend')
        parser.add_argument('--mock-pi', action='store_true', help='Use mock implementation instead of GPIO')
        parser.add_argument('--mock-server', action='store_true', help='Use mock backend')
        parser.add_argument('--mock-init', help='Initial JSON state of mock')
        parser.add_argument('--mock-print', action='store_true', help='Print mock state changes')
        parser.add_argument('--run-interval', type=float, help='Override running interval (secs, float)')
        parser.add_argument('--poll-interval', type=float, help='Override polling interval (secs, float)')
        parser.add_argument('--write-interval', type=float, help='Override writing interval (secs, float)')
        parser.add_argument('--url',  help='Define target serve base URL in format <protocol>://<ipaddress>:<port>')
        parser.add_argument('--verbose', action='store_true', help='Print verbose messages during operation')
        parser.add_argument('--proxy', help='Use proxy for connections')

        args = parser.parse_args()
        if args.id:
            options['id'] = args.id
        if args.mock_pi:
            options['mock_pi'] = True
        if args.mock_server:
            options['mock_server'] = True
        if args.mock_print:
            options['mock_print'] = True
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
            options['print'] = True
        if args.proxy:
            options['proxy'] = args.proxy
        
        