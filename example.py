
from odysseus import log
from odysseus.taskbox import *
import random
import time

# Usage:  python3 example.py --id myid --mock-server --mock-init '{"number":7}'

def logic(state, backend_change):
    if state is not None:
        if backend_change:
            print("Backend changed to: number=" + str(state.get("number", None)))
        else:
            if random.random() > 0.5:
                state["number"] = state["number"] + 1
                print("number=" + str(state.get("number", None)))
    return state

def box_init():
    print("Init called")


options = {
    "callback": logic,
    "run_interval": 0.3,
    "initial_state": { "number": 0 },
    # "write_interval": 2
    # "init":box_init,
    # "mock_init":box_init
}

TaskBoxRunner(options).run()
