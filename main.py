"""Interview Assistant — entry point.

Flow:
  1. Splash window appears with a Start button.
  2. Clicking Start runs a background thread that imports the heavy modules
     (openai, anthropic, soundcard, numpy, google-auth, app) while a progress
     bar advances.
  3. Splash closes. The Google login window opens; after success, the main
     overlay launches.

Keeping this file's top-level imports light is intentional — only logging
and the splash class itself. The heavy ones happen behind the progress bar.
"""
import sys
from logger_setup import setup_logging

log = setup_logging()


def _run():
    log.info("=" * 60)
    log.info("Interview Assistant starting")

    from splash import SplashWindow
    if not SplashWindow().run():
        log.info("Splash cancelled — exiting")
        return

    # By the time we get here the splash loader has warmed sys.modules for
    # `auth` and `app`, so these imports are cache hits and effectively free.
    from auth import LoginWindow
    if not LoginWindow().run():
        log.info("Login cancelled / failed — exiting")
        return

    log.info("Login OK — launching main window")
    from app import InterviewAssistant
    InterviewAssistant()


if __name__ == "__main__":
    try:
        _run()
    except Exception:
        log.exception("Fatal error at startup")
        raise
    finally:
        sys.exit(0)
