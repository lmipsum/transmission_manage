"""Logging setup: console plus an optional rotating log file, with passkeys redacted everywhere."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from modules.util import RedactingFilter

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(message)s"


def setup_logging(level="INFO", log_file=None, log_size_mb=10, log_count=5):
    """Configure the "transmission_manage" logger and return it."""
    logger = logging.getLogger("transmission_manage")
    logger.handlers.clear()
    logger.setLevel(level.upper() if isinstance(level, str) else level)
    logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT)
    redacting = RedactingFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(redacting)
    logger.addHandler(console)

    if log_file:
        directory = os.path.dirname(os.path.abspath(log_file))
        os.makedirs(directory, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=log_size_mb * 1024 * 1024, backupCount=log_count, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        rotating.addFilter(redacting)
        logger.addHandler(rotating)
    return logger
