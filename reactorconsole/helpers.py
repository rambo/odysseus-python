"""Shared helpers"""
import functools
import logging


def log_exceptions(func, re_raise=True):
    """Decorator to log exceptions that are easy to lose in callbacks"""

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # pylint: disable=W0703
            logging.getLogger().exception(exc)
            if re_raise:
                raise exc
    return wrapped
