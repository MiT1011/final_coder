"""Google OAuth login + DPAPI-encrypted token storage.

The OAuth app credentials (`client_secret.json`) are bundled inside the
PyInstaller .exe and read from `sys._MEIPASS` at runtime — they never land
in APPDATA. The resulting user token is encrypted via DPAPI before being
written to disk, so even the buyer of the .exe cannot recover it from
filesystem snooping.

Legacy plaintext `google_token.json` files from earlier installs are
auto-migrated to encrypted `google_token.dat` and the legacy file is removed.
"""
import os
import sys
import json
import logging
import threading
import tkinter as tk
from tkinter import messagebox

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from secure_store import load_secret_json, save_secret_json

log = logging.getLogger(__name__)

SCOPES = [
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
]

APP_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "InterviewAssistant")
TOKEN_FILE = os.path.join(APP_DIR, "google_token.dat")          # encrypted
_LEGACY_TOKEN_FILE = os.path.join(APP_DIR, "google_token.json")  # plaintext from older builds
_LEGACY_CLIENT_SECRET = os.path.join(APP_DIR, "client_secret.json")  # never written, but clean if present


def _bundle_dir():
    """Where read-only bundled resources live (works for both dev and frozen)."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _client_secret_path():
    """The OAuth app credentials. Bundled-only — never copied to APPDATA."""
    return os.path.join(_bundle_dir(), "client_secret.json")


def _cleanup_legacy_files():
    """Migrate legacy plaintext token; remove any stray client_secret in APPDATA."""
    if os.path.isfile(_LEGACY_TOKEN_FILE):
        try:
            with open(_LEGACY_TOKEN_FILE, "r") as f:
                data = json.load(f)
            save_secret_json(TOKEN_FILE, data)
            os.remove(_LEGACY_TOKEN_FILE)
            log.info("Migrated legacy google_token.json -> google_token.dat (DPAPI)")
        except Exception:
            log.exception("Legacy token migration failed")
    if os.path.isfile(_LEGACY_CLIENT_SECRET):
        # An old build may have copied client_secret.json to APPDATA. Wipe it —
        # we always load from _MEIPASS now.
        try:
            os.remove(_LEGACY_CLIENT_SECRET)
            log.info("Removed stale client_secret.json from APPDATA")
        except Exception:
            log.exception("Could not remove stale client_secret.json")


def _load_creds():
    """Read+decrypt stored credentials. Returns None if missing or unreadable."""
    _cleanup_legacy_files()
    data = load_secret_json(TOKEN_FILE)
    if not data:
        return None
    try:
        return Credentials.from_authorized_user_info(data, SCOPES)
    except Exception:
        log.exception("Credentials.from_authorized_user_info failed")
        return None


def _save_creds(creds):
    """Encrypt and persist credentials."""
    os.makedirs(APP_DIR, exist_ok=True)
    save_secret_json(TOKEN_FILE, json.loads(creds.to_json()))


class LoginWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Interview Assistant - Login")
        self.root.geometry("400x250")
        self.root.configure(bg="#0d1117")
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws / 2) - (w / 2)
        y = (hs / 2) - (h / 2)
        self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))

        self.success = False

        tk.Label(self.root, text="Welcome to Interview Assistant", bg="#0d1117",
                 fg="#58a6ff", font=("Segoe UI", 14, "bold")).pack(pady=(30, 10))
        tk.Label(self.root, text="Please login with Google to continue.",
                 bg="#0d1117", fg="#8b949e", font=("Segoe UI", 10)).pack(pady=(0, 20))

        self.login_btn = tk.Button(self.root, text="Login with Google",
                                    command=self.start_login, bg="#21262d",
                                    fg="#e6edf3", font=("Segoe UI", 10, "bold"),
                                    relief="flat", padx=20, pady=8)
        self.login_btn.pack()

        self.status_lbl = tk.Label(self.root, text="", bg="#0d1117",
                                    fg="#8b949e", font=("Segoe UI", 9))
        self.status_lbl.pack(pady=10)

        self.root.after(100, self._auto_login_check)

    def _auto_login_check(self):
        creds = _load_creds()
        if not creds:
            return
        try:
            if creds.valid:
                self.success = True
                self.root.destroy()
                return
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                _save_creds(creds)
                self.success = True
                self.root.destroy()
        except Exception:
            log.exception("Auto-login check failed")

    def start_login(self):
        self.login_btn.config(state="disabled", text="Logging in...")
        self.status_lbl.config(text="Check your browser...")
        threading.Thread(target=self._perform_auth, daemon=True).start()

    def _perform_auth(self):
        creds = _load_creds()
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    log.exception("Refresh failed — falling back to browser flow")
                    creds = self._do_browser_login()
            else:
                creds = self._do_browser_login()
            if creds:
                try:
                    _save_creds(creds)
                except Exception:
                    log.exception("Failed to persist credentials")

        if creds and creds.valid:
            self.success = True
            self.root.after(0, self._on_success)
        else:
            self.root.after(0, self._on_fail)

    def _do_browser_login(self):
        try:
            path = _client_secret_path()
            if not os.path.exists(path):
                self.root.after(0, lambda: messagebox.showerror(
                    "Error", "client_secret.json not found in bundle!"))
                log.error("client_secret.json missing from bundle: %s", path)
                return None
            flow = InstalledAppFlow.from_client_secrets_file(path, SCOPES)
            return flow.run_local_server(port=0)
        except Exception as e:
            log.exception("Browser login failed")
            self.root.after(0, lambda: messagebox.showerror("Login Error", str(e)))
            return None

    def _on_success(self):
        self.status_lbl.config(text="Login successful!", fg="#3fb950")
        self.root.after(500, self.root.destroy)

    def _on_fail(self):
        self.login_btn.config(state="normal", text="Login with Google")
        self.status_lbl.config(text="Login failed or cancelled.", fg="#f85149")

    def run(self):
        self.root.mainloop()
        return self.success
