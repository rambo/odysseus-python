
from odysseus import log
from odysseus.taskbox import TaskBoxRunner
import pigpio
import time
import random

# Usage:  python3 fuses.py --id myid --mock-server --mock-print


# Monitors which GPIO pins are connected to one another.  Performs a full
# pin-to-pin mapping of connectivity.
#
# Output:
# state["fuses"] is an array of 0 for blown or 1 for intact fuse (in order of "measure" array)
#
# Input:
# state["blow"] is an array of fuses to blow (from 0-n, same index as in other arrays)
#
# Config:
# state["config"]["measure"] is an array of measurement pins
# state["config"]["blowing"] is an array of pins for blowing fuses
#
# See https://docs.google.com/presentation/d/1nHQT-9P4XJcRAOwAHvOXL8Q1mKtLc4B5tvqKxWRZOsg/edit#slide=id.g4a2cf36468_0_1352


pi = pigpio.pi()

BLOW_TIME=0.25
SAFETY_DELAY=0.05
CALLS_PER_SECOND=2

default_state = {
    "fuses": [],
    "config": {
        "blowing": [2,  4, 15, 18, 22, 24,  9, 11, 7,  6, 13, 16],
        "measure": [3, 14, 17, 27, 23, 10, 25,  8, 5, 12, 19, 26],
    },
    "presets": {
        "blow_one": {
            "blow": [0]
        },
        "blow_all": {
            "blow": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
        },
    }
}



def logic(state, backend_change):
    if backend_change:
        init_pins(state["config"])
        if state.get("blow", None):
            blow_fuses(state["blow"], state["config"])
            state.pop("blow", None)
    
    state["fuses"] = read_fuses(state["config"]["measure"])
    return state


def init_pins(config):
    for i in config["blowing"]:
        pi.set_mode(i, pigpio.OUTPUT)
        pi.write(i, 0)
    for i in config["measure"]:
        pi.set_mode(i, pigpio.INPUT)
        pi.set_pull_up_down(i, pigpio.PUD_OFF)


def read_fuses(pins):
    result = []
    for i in pins:
        result.append(pi.read(i))
    return result


def blow_fuses(indexes, config):
    # For added safety, set measurement pins to low-impedance state (output low) when blowing
    for i in config["measure"]:
        pi.set_mode(i, pigpio.OUTPUT)
        pi.write(i, 0)
    time.sleep(SAFETY_DELAY)
    random.shuffle(indexes)
    for i in indexes:
        if i >= 0 and i < len(config["blowing"]):
            pin = config["blowing"][i]
            pi.set_mode(pin, pigpio.OUTPUT)
            pi.write(pin, 1)
            time.sleep(BLOW_TIME)
            pi.write(pin, 0)
            time.sleep(SAFETY_DELAY)
    init_pins(config)



options = {
    "callback": logic,
    "run_interval": 1.0 / CALLS_PER_SECOND,
    "initial_state": default_state,
}
TaskBoxRunner(options).run()
