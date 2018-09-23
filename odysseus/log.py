import sys
import traceback

def exception_handler(exctype, exception, tb):
    error(exctype.__name__ + ": " + str(exception), exception)

sys.excepthook = exception_handler

def error(msg, exception=None, data=None):
    """Perform error logging locally + to the remote server."""
    print("ERROR: " + msg)
    if exception:
        print(''.join(traceback.format_tb(exception.__traceback__)))
    if data:
        print(data)
