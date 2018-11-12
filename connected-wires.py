
from odysseus import log
from odysseus.taskbox import TaskBoxRunner
import pigpio

# Usage:  python3 connected-wires.py --id myid --mock-server --mock-print


# Monitors which GPIO pins are connected to one another.  Performs a full
# pin-to-pin mapping of connectivity.
#
# state["config"]["pins"] defines which pins are monitored.


pi = pigpio.pi()

CALLS_PER_SECOND=2

default_state = {
    "connected": {},
    "config": {
        "pins": [4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27]
    }
}

def logic(state, backend_change):
    if backend_change:
        init_pins(state["config"]["pins"])
    
    state["connected"] = read_pins(state["config"]["pins"])
    return state


def init_pins(pins):
    for i in pins:
        pi.set_mode(i, pigpio.INPUT)
        pi.set_pull_up_down(i, pigpio.PUD_DOWN)

def read_pins(pins):
    conns = {}
    for i in pins:
        pi.set_mode(i, pigpio.OUTPUT)
        pi.write(i, 1)
        vals = pi.read_bank_1()
        pi.set_mode(i, pigpio.INPUT)
        pi.set_pull_up_down(i, pigpio.PUD_DOWN)
        #print("i=" + str(i) + " vals=" + '{:032b}'.format(vals))
        for j in pins:
            if i >= j:
                continue
            if (1<<j) & vals:
                conns[(i,j)] = True
    return conns



options = {
    "callback": logic,
    "run_interval": 1.0 / CALLS_PER_SECOND,
    "initial_state": default_state,
}
TaskBoxRunner(options).run()
