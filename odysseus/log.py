import sys
import traceback

def exception_handler(exctype, exception, tb):
    error("" + exctype.__name__ + ": " + str(exception), exception)

sys.excepthook = exception_handler

# TODO: This should be changed to use Python standard logging
# mechanism and probably use a customized HTTPHandler:
# https://docs.python.org/2/library/logging.handlers.html#logging.handlers.HTTPHandler

def error(msg, exception=None, data=None):
    """Perform error logging locally + to the remote server."""
    print("ERROR: " + msg)
    if exception:
        print(''.join(traceback.format_tb(exception.__traceback__)))
    if data:
        print(data)
