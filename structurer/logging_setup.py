"""Logs: console (journalctl) + rotating file (logs/app.log)."""
import logging
import os
from logging.handlers import RotatingFileHandler

from . import config


def _setup():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    lg = logging.getLogger("structurer")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.StreamHandler(),
              RotatingFileHandler(os.path.join(config.LOG_DIR, "app.log"),
                                  maxBytes=5_000_000, backupCount=5)):
        h.setFormatter(fmt)
        lg.addHandler(h)
    return lg


log = _setup()


def mask(phone):
    """Logs mein poora phone nahi, sirf ***246."""
    return ("***" + phone[-3:]) if phone else None
