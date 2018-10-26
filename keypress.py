#!/usr/bin/python3

import sys
import select
import time

def pollLine():
    """Return an typed line (after pressing enter) without blocking.
    Returns None if enter has not been pressed."""
    i,o,e = select.select([sys.stdin],[],[],0.0001)
    for s in i:
        if s == sys.stdin:
            input = sys.stdin.readline()
            return input
    return None

_prevChar = ""
_prevReturn = None
_prevTime = 0
def pollChar(sticky):
    """Returns the last char typed before pressing enter each time
    enter has been pressed. Returns None if enter has not been pressed.
    If `sticky` param is True, will return the same response for 0.1 secs."""
    global _prevChar
    global _prevReturn
    global _prevTime

    if sticky and (time.time() - _prevTime) < 0.1:
        return _prevReturn

    line = pollLine()
    _prevTime = time.time()
    if line:
        line = line.strip()
        if len(line) > 0:
            _prevChar = line[-1:]
        _prevReturn = _prevChar
        return _prevChar
    _prevReturn = None
    return None


if __name__ == '__main__':
    count = 0
    while True:
        line = pollChar(False)
        if line:
            print("READ: " + line)
        else:
            count += 1
            print(count)
        time.sleep(0.1)
