
from odysseus import backend
from odysseus import log
from odysseus.taskbox import *
import random
import time

# Usage:  python3 example.py --id myid --mock --mock-init '{"number":7}'

def logic(state):
    if random.random() > 0.5:
        state["number"] = state.get("number", 0) + 1
    print("number=" + str(state.get("number", None)))
    return state

options = {
    "callback": logic,
    "interval": 0.5,
}
run_task_box(options)
