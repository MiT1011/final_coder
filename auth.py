import os
import json
import tkinter as tk
from tkinter import messagebox
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import threading

# Scopes required for getting the user's email
SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile']

APP_DIR = os.path.join(os.getenv("APPDATA", os.path.expanduser("~")), "InterviewAssistant")
TOKEN_FILE = os.path.join(APP_DIR, "google_token.json")

class LoginWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Interview Assistant - Login")
        self.root.geometry("400x250")
        self.root.configure(bg="#0d1117")
        # Center the window
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws/2) - (w/2)
        y = (hs/2) - (h/2)
        self.root.geometry('%dx%d+%d+%d' % (w, h, x, y))
        
        self.success = False

        tk.Label(self.root, text="Welcome to Interview Assistant", bg="#0d1117", fg="#58a6ff", font=("Segoe UI", 14, "bold")).pack(pady=(30, 10))
        tk.Label(self.root, text="Please login with Google to continue.", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 10)).pack(pady=(0, 20))

        self.login_btn = tk.Button(self.root, text="Login with Google", command=self.start_login, bg="#21262d", fg="#e6edf3", font=("Segoe UI", 10, "bold"), relief="flat", padx=20, pady=8)
        self.login_btn.pack()

        self.status_lbl = tk.Label(self.root, text="", bg="#0d1117", fg="#8b949e", font=("Segoe UI", 9))
        self.status_lbl.pack(pady=10)

        # Check if already valid token exists so we don't even need to click
        self.root.after(100, self._auto_login_check)

    def _auto_login_check(self):
        if os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
                if creds and creds.valid:
                    self.success = True
                    self.root.destroy()
                elif creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(TOKEN_FILE, 'w') as token:
                        token.write(creds.to_json())
                    self.success = True
                    self.root.destroy()
            except Exception:
                pass

    def start_login(self):
        self.login_btn.config(state="disabled", text="Logging in...")
        self.status_lbl.config(text="Check your browser...")
        threading.Thread(target=self._perform_auth, daemon=True).start()

    def _perform_auth(self):
        creds = None
        if os.path.exists(TOKEN_FILE):
            try:
                creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            except:
                pass

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = self._do_browser_login()
            else:
                creds = self._do_browser_login()

            if creds:
                os.makedirs(APP_DIR, exist_ok=True)
                with open(TOKEN_FILE, 'w') as token:
                    token.write(creds.to_json())

        if creds and creds.valid:
            self.success = True
            self.root.after(0, self._on_success)
        else:
            self.root.after(0, self._on_fail)

    def _do_browser_login(self):
        try:
            client_secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'client_secret.json')
            if not os.path.exists(client_secret_path):
                self.root.after(0, lambda: messagebox.showerror("Error", "client_secret.json not found!"))
                return None
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, SCOPES)
            creds = flow.run_local_server(port=0)
            return creds
        except Exception as e:
            print("Login Error:", e)
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
