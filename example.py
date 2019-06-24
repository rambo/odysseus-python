
from odysseus import log
from odysseus.taskbox import *
import random

# Usage:  python3 example.py --id myid --mock-server

# Simulating backend value change:  echo '{"number":5}' > backend-mock-myid.json

def logic(state, backend_change):
    if backend_change:
        print(state)
        print("Backend changed to: number=" + str(state.get("number", None)))
    else:
        if random.random() > 0.5:
            state["number"] = state["number"] + 1
            print("number=" + str(state.get("number", None)))
    return state

def box_init():
    print("Init called")


options = {
    "init":box_init,
    "callback": logic,
    "run_interval": 0.3,
    "initial_state": { "number": 0 },
    # "write_interval": 2
    # "mock_init":box_init
}

TaskBoxRunner(options).run()
