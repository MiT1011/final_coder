"""Centralised logging — rotating file in %APPDATA%\\InterviewAssistant\\app.log + console."""
import logging, os, sys
from logging.handlers import RotatingFileHandler

_APP_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "InterviewAssistant")
LOG_FILE = os.path.join(_APP_DIR, "app.log")

_initialized = False

def setup_logging(level=logging.INFO):
    """Initialise root logger once. Safe to call multiple times."""
    global _initialized
    if _initialized:
        return logging.getLogger("InterviewAssistant")

    try:
        os.makedirs(_APP_DIR, exist_ok=True)
    except Exception as e:
        print(f"[logger] Could not create log dir: {e}", file=sys.stderr)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s %(threadName)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)
    # Wipe any inherited handlers so we don't double-log.
    root.handlers = []

    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt); fh.setLevel(level)
        root.addHandler(fh)
    except Exception as e:
        print(f"[logger] File handler failed: {e}", file=sys.stderr)

    # NOTE: console (StreamHandler) intentionally omitted — logs go to file only.

    # Quiet down very noisy third-party loggers.
    for noisy in ("urllib3", "httpx", "httpcore", "PIL", "google", "google_auth_oauthlib", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _initialized = True
    log = logging.getLogger("InterviewAssistant")
    log.info("Logging initialised → %s", LOG_FILE)
    return log
