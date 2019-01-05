
from odysseus import log
from odysseus.taskbox import *
import random
from pygame import time 
import sys
import launchpad_py as launchpad

global lp
mode = None

def add_values_cross(state, button):
    global lp 

    if (button % 8) - 1 > int(state['limits'][0]):
        state[str(button-1)] = (state[str(button-1)]+1) % 2
    if (button % 8) + 1 < int(state['limits'][1]):
        state[str(button+1)] = (state[str(button+1)]+1) % 2 
    if button-16 > int(state['limits'][2]):
        state[str(button-16)] = (state[str(button-16)]+1) % 2
    if button+16 < int(state['limits'][3]):
        state[str(button+16)] = (state[str(button+16)]+1) % 2 
    
    return state 

def print_grid(state):
    for i in range(8):
        for y in range(8):
            button = y+i*16
            if state[str(button)] == 0:
                lp.LedCtrlRaw(button, 0, 0)
            if state[str(button)] == 1:
                lp.LedCtrlRaw(button, 3, 0)
            if state[str(button)] == 2:
                lp.LedCtrlRaw(button, 0, 3)

def set_limits(state):
    for i in range(8):
        for y in range(8):
            if y < state['limits'][0] or y > state['limits'][1] or i*16 < state['limits'][2] or i*16 > state['limits'][3]:
                button = y+i*16
                state[str(button)] = 2
    
    return state 
    



def logic(state, backend_change):
    global lp 

    if backend_change:
        lp.Reset()
        state=set_limits(state)
        print_grid(state)

    but = None
    if mode == "XL" or mode == "LKM":
        but = lp.InputStateRaw()
    else:
        but = lp.ButtonStateRaw()

    if but:
        if but[1] == False:
            state = add_values_cross(state, but[0])
            print_grid(state)
    return state


def box_init():
    # create an instance
    global lp
    lp = launchpad.Launchpad();

	# check what we have here and override lp if necessary

    if lp.Check( 0, "pro" ):
        lp = launchpad.LaunchpadPro()
        if lp.Open(0,"pro"):
            print("Launchpad Pro")
            mode = "Pro"
    
    elif lp.Check( 0, "mk2" ):
        lp = launchpad.LaunchpadMk2()
        if lp.Open( 0, "mk2" ):
            print("Launchpad Mk2")
            mode = "Mk2"
    
    elif lp.Check( 0, "control xl" ):
        lp = launchpad.LaunchControlXL()
        if lp.Open( 0, "control xl" ):
            print("Launch Control XL")
            mode = "XL"
            
    elif lp.Check( 0, "launchkey" ):
        lp = launchpad.LaunchKeyMini()
        if lp.Open( 0, "launchkey" ):
            print("LaunchKey (Mini)")
            mode = "LKM"
            
    elif lp.Check( 0, "dicer" ):
        lp = launchpad.Dicer()
        if lp.Open( 0, "dicer" ):
            print("Dicer")
            mode = "Dcr"
    
    else:
        if lp.Open():
            print("Launchpad Mk1/S/Mini")
            mode = "Mk1"
            
    if mode is None:
        print("Did not find any Launchpads, meh...")
        return

    lp.Reset()
    


options = {
    "callback": logic,
    "run_interval": 0.1,
    "initial_state": {'0': 0, '1': 0, '2': 0, '3': 0, '4': 0, '5': 0, '6': 0, '7': 0, '16': 0, '17': 0, '18': 0, '19': 0, '20': 0, '21': 0, '22': 0, '23': 0, '32': 0, '33': 0, '34': 0, '35': 0, '36': 0, '37': 0, '38': 0, '39': 0, '48': 0, '49': 0, '50': 0, '51': 0, '52': 0, '53': 0, '54': 0, '55': 0, '64': 0, '65': 0, '66': 0, '67': 0, '68': 0, '69': 0, '70': 0, '71': 0, '80': 0, '81': 0, '82': 0, '83': 0, '84': 0, '85': 0, '86': 0, '87': 0, '96': 0, '97': 0, '98': 0, '99': 0, '100': 0, '101': 0, '102': 0, '103': 0, '112': 0, '113': 0, '114': 0, '115': 0, '116': 0, '117': 0, '118': 0, '119': 0, 'set':False, 'limits':[-1,8,-1,120]},
    # "write_interval": 2
    "init":box_init,
    "mock_init":box_init
}

TaskBoxRunner(options).run()
lp.reset()
lp.close() 

