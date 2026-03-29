import logging
import os
from logging.handlers import TimedRotatingFileHandler

import config


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)

    log_path = os.path.join(config.LOG_DIR, "scraper.log")
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=config.LOG_RETENTION_DAYS
    )
    file_handler.setFormatter(fmt)
    file_handler.suffix = "%Y-%m-%d"

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    return logging.getLogger("deal-finder")
