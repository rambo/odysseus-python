
from odysseus import log
from odysseus.taskbox import *
import random
import time
import math

# Usage:  python3 drifting-value.py --id myid --mock --mock-init '{"number":7}'


# Displays a drifting value with randomness. Value can be controlled
# by external inputs.
#
# Displayed value is a combination of:
# - an actual value which drifts with a constant speed
# - a brownian noise component (clamped)
# - a sine wave
# - white noise which decays with time (set to high value when controlled)


CALLS_PER_SECOND=10


default_state = {
    "value": 330.5,         # current "real" value
    "rndMagnitude": 30,      # current magnitude of white noise
    "brownNoiseValue": 0,   # current brown noise value
    "drift": 3,             # value drift per MINUTE
    "sinePosition": 0,      # sine wave position
    "config": {
        "maxRndMagnitude": 30,      # maximum white noise value when updating value
        "rndMagnitudeDecay": 0.95,  # how fast random magnitude decays
        "brownNoiseSpeed": 0.1,     # magnitude how fast brown noise changes
        "brownNoiseMax": 10,        # maximum absolute value of brownian noise
        "sineMagnitude": 1,         # magnitude of sine wave
        "sineSpeed": 60,            # sine cycle time in seconds
        "minDriftPerMinute": 2,     # minimum drift per MINUTE when randomizing drift
        "maxDriftPerMinute": 4,     # maximum drift per MINUTE when randomizing drift
    }
}

def logic(state, backend_change):
    if backend_change:
        print("BACKEND CHANGE")
        return None
    # Update values
    state["value"] += state["drift"] / CALLS_PER_SECOND / 60
    state["rndMagnitude"] = state["rndMagnitude"] * state["config"]["rndMagnitudeDecay"]
    bnSpeed = state["config"]["brownNoiseSpeed"]
    bnValue = state["brownNoiseValue"] + random.uniform(-bnSpeed, bnSpeed)
    bnMax = state["config"]["brownNoiseMax"]
    state["brownNoiseValue"] = min(bnMax, max(-bnMax, bnValue))
    state["sinePosition"] += 2 * math.pi / state["config"]["sineSpeed"] / CALLS_PER_SECOND
    if state["sinePosition"] > 2 * math.pi:
        state["sinePosition"] -= 2 * math.pi

    # Print value
    sine = math.sin(state["sinePosition"]) * state["config"]["sineMagnitude"]
    #rnd = random.uniform(-state["rndMagnitude"], state["rndMagnitude"])
    rnd = random.gauss(0, state["rndMagnitude"])
    value = state["value"] + state["brownNoiseValue"] + rnd + sine

    # display  actual  brown  sine  white-mag
    print("{:.1f}\t{:.2f}\t{:+.2f}\t{:+.2f}\t{:.2f}".format(value, state["value"], state["brownNoiseValue"], sine, state["rndMagnitude"]))
    return state

options = {
    "callback": logic,
    "run_interval": 1.0 / CALLS_PER_SECOND,
    "write_interval": 10,
    "initial_state": default_state,
}
TaskBoxRunner(options).run()
