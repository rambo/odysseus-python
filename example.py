
from odysseus import backend
from odysseus import log
from odysseus.taskbox import *
import random
import time

# Usage:  python3 example.py --id myid --mock --mock-init '{"number":7}'

def logic(state, backend_change):
    if backend_change:
        print("Backend changed to: number=" + str(state.get("number", None)))
    else:
        if random.random() > 0.5:
            state["number"] = state.get("number", 0) + 1
        print("number=" + str(state.get("number", None)))
    return state

options = {
    "callback": logic,
    "run_interval": 0.3,
#    "write_interval": 2
}
TaskBoxRunner(options).run()
