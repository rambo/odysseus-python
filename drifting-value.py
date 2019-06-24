
from odysseus import log
from odysseus.taskbox import *
import random
import time
import math
import keypress
import microdotphat

# Prerequisite:
#   sudo apt-get install python3-numpy python3-smbus
#   pip3 install microdotphat
# Usage:  python3 drifting-value.py --id myid --mock-pi --mock-server


# Displays a drifting value with randomness. Value can be adjusted
# by external inputs.
#
# Displayed value is a combination of:
# - an actual value which drifts with a constant speed (which is paused for a while after adjusting)
# - a brownian noise component (clamped)
# - a sine wave
# - white noise which decays with time (set to high value when adjusted)


CALLS_PER_SECOND=10

SWITCH_GPIO_PIN=4

default_state = {
    "value": 330.5,         # current "real" value
    "displayValue": 330.5   # displayed value
    "rndMagnitude": 0,      # current magnitude of white noise
    "brownNoiseValue": 0,   # current brown noise value
    "drift": 3,             # value drift per MINUTE
    "driftPause": 0,        # drift pause secs remaining
    "sinePosition": 0,      # sine wave position
    "minDriftPerMinute": 2, # minimum drift per MINUTE when randomizing drift
    "maxDriftPerMinute": 4, # maximum drift per MINUTE when randomizing drift
    "config": {
        "maxRndMagnitude": 30,       # maximum white noise value when updating value
        "rndMagnitudeDecay": 0.95,   # how fast random magnitude decays (stabilation time: 0.95 ~5s, 0.97 ~10s)
        "brownNoiseSpeed": 0.1,      # magnitude how fast brown noise changes
        "brownNoiseMax": 10,         # maximum absolute value of brownian noise
        "sineMagnitude": 1,          # magnitude of sine wave
        "sineSpeed": 60,             # sine cycle time in seconds
        "driftDelayAfterAdjust": 60, # number of secs to pause drift after adjustment is done
        "adjustUpAmount": 0.3,       # amount to adjust up per call (pressure rise)
        "adjustDownAmount": 1,       # amount to adjust down per call (pressure release)
    }
}

def logic(state, backend_change):
    if backend_change:
        print("BACKEND CHANGE")
        return None
    
    config = state["config"]

    # Update values
    state["driftPause"] = max(state["driftPause"] - 1.0/CALLS_PER_SECOND, 0)
    if state["driftPause"] == 0:
        state["value"] += state["drift"] / CALLS_PER_SECOND / 60
    state["rndMagnitude"] = state["rndMagnitude"] * config["rndMagnitudeDecay"]
    bnSpeed = config["brownNoiseSpeed"]
    bnValue = state["brownNoiseValue"] + random.uniform(-bnSpeed, bnSpeed)
    bnMax = config["brownNoiseMax"]
    state["brownNoiseValue"] = min(bnMax, max(-bnMax, bnValue))
    state["sinePosition"] += 2 * math.pi / config["sineSpeed"] / CALLS_PER_SECOND
    if state["sinePosition"] > 2 * math.pi:
        state["sinePosition"] -= 2 * math.pi

    # Check whether we are adjusting the value
    adjustment = getAdjustment(config)
    if adjustment:
        state["value"] += adjustment
        state["driftPause"] = config["driftDelayAfterAdjust"]
        state["rndMagnitude"] = config["maxRndMagnitude"]
        state["drift"] = random.uniform(state["minDriftPerMinute"], state["maxDriftPerMinute"])
        if random.random() < 0.5:
            state["drift"] = -state["drift"]


    # Print value
    sine = math.sin(state["sinePosition"]) * config["sineMagnitude"]
    #rnd = random.uniform(-state["rndMagnitude"], state["rndMagnitude"])
    rnd = random.gauss(0, state["rndMagnitude"])
    value = state["value"] + state["brownNoiseValue"] + rnd + sine
    state["displayValue"] = value

    # display  actual  brown  sine  white-mag
    #print("{:.1f}\t{:.2f}\t{:+.2f}\t{:+.2f}\t{:.2f}".format(value, state["value"], state["brownNoiseValue"], sine, state["rndMagnitude"]))

    microdotphat.write_string("{:.2f}".format(value), kerning=False)
    microdotphat.show()

    return state

getAdjustment = None


## Mock implementation

def init_mock():
    global getAdjustment
    getAdjustment = getAdjustmentMock


def getAdjustmentMock(config):
    c = keypress.pollChar(False)
    if c == "u":
        return config["adjustUpAmount"]
    elif c == "d":
        return -config["adjustDownAmount"]
    else:
        return None


## Real implementation

pi = None
i2c_handle = None
def init():
    import pigpio
    global pi
    global getAdjustment
    global i2c_handle

    pi = pigpio.pi()
    getAdjustment = getAdjustmentReal

    microdotphat.clear()

    i2c_handle = pi.i2c_open(1, 0x28)

    pi.set_mode(SWITCH_GPIO_PIN, pigpio.INPUT)
    pi.set_pull_up_down(SWITCH_GPIO_PIN, pigpio.PUD_UP)
    pi.set_glitch_filter(SWITCH_GPIO_PIN, 200000) # Report change only after 200ms steady


def getAdjustmentReal(config):
    p = readPressure()
    if p > 110:
        return -config["adjustDownAmount"]
    
    v = pi.read(SWITCH_GPIO_PIN)
    if v == 0:
        return config["adjustUpAmount"]
    return None


def readPressure():
    pressure_range = 206.843
    (c, data) = pi.i2c_read_i2c_block_data(i2c_handle, 0x28, 2)
    if len(data) < 2:
        print("COULD NOT READ BYTES FROM SENSOR")
        return 0
    value = data[0]*256 + data[1]
    percentage = (value / 0x3FFF)*100
    kpa = ( percentage - 10 ) * pressure_range / 80
    return kpa


options = {
    "init": init,
    "init_mock": init_mock,
    "callback": logic,
    "run_interval": 1.0 / CALLS_PER_SECOND,
    "write_interval": 10,
    "initial_state": default_state,
}
TaskBoxRunner(options).run()
