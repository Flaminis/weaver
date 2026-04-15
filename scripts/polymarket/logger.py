from __future__ import annotations
import logging
import sys

_FMT = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s"
_DATE_FMT = "%H:%M:%S"


def get_logger(name: str, level=logging.DEBUG) -> logging.Logger:
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
        log.addHandler(h)
    log.setLevel(level)
    return log
