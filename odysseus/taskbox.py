import time
import copy
import argparse
import json

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

    def read(self):
        print("READ(" + self.id + "):  " + str(self.state))
        return self.state

    def write(self, newState):
        self.state = newState
        print("WRITE(" + self.id + "): " + str(self.state))


def run_task_box(options):
    _parse_command_line(options)
    _validate(options)

    if options['mock']:
        box = MockTaskBox(options['id'], options.get('mock_init', {}))
    else:
        box = TaskBox(options['id'])

    callback = options['callback']
    interval = options['interval']
    state = box.read()
    nextRunTime = time.time()
    while True:
        _wait_until(nextRunTime)
        newState = callback(copy.deepcopy(state))
        if newState != None and newState != state:
            box.write(newState)
            state = newState
        nextRunTime = nextRunTime + interval
        if nextRunTime < time.time():
            nextRunTime = time.time()


def _wait_until(t):
    while (t > time.time()):
        time.sleep(t - time.time())


def _validate(options):
    options['mock'] = options.get('mock', False)
    if 'id' not in options:
        raise Exception("'id' is not defined, use --id <myid>")
    if 'callback' not in options:
        raise Exception("'callback' is not defined")
    if 'interval' not in options:
        raise Exception("'interval' is not defined")


def _parse_command_line(options):
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
