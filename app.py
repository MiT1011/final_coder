"""Interview Assistant — unified stealth chat overlay.

One chat surface for everything: typed text, transcribed audio, and optional
screenshot attachments. The interviewer (screen-sharing) sees nothing.

Send path:
- Text typed in the input area -> active LLM (text).
- Audio transcript (auto or manual) -> posted as an "Interviewer" message;
  the active LLM replies. If screenshots are attached, vision API is used.
- 📸 Capture (Alt+G) attaches a screenshot to the next message. Attached
  screenshots are visible as thumbnails above the input. They're consumed
  on send and cleared after.

Security:
- API keys are stored DPAPI-encrypted in %APPDATA%\\InterviewAssistant.
- The Google OAuth token is also DPAPI-encrypted; client_secret.json is
  bundled inside the .exe and never copied to APPDATA.
"""
import tkinter as tk
from tkinter import scrolledtext, simpledialog, messagebox
import threading, sys, ctypes, atexit, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from PIL import ImageGrab, ImageTk

from win_hotkeys import HotkeyManager
import config as cfg
from logger_setup import setup_logging
log = setup_logging()
from llm_service import (
    LLMService, SessionManager,
    img_to_b64, load_api_keys, save_api_keys, get_api_key, transcribe_audio,
    available_models,
)
try:
    from audio_service import AudioListener, AudioCaptureError, ManualRecorder, chunk_wav_for_whisper
    AUDIO_IMPORT_ERROR = None
except Exception as _audio_imp_err:
    log.exception("audio_service import failed")
    AudioListener = None
    AudioCaptureError = Exception
    ManualRecorder = None
    chunk_wav_for_whisper = None
    AUDIO_IMPORT_ERROR = str(_audio_imp_err)


# ----- Win32 stealth + click-through helpers ----------------------------------
def _win_hwnd(title):
    if sys.platform == "win32":
        return ctypes.windll.user32.FindWindowW(None, title)
    return 0


def apply_stealth(hwnd):
    """WDA_EXCLUDEFROMCAPTURE — the window is omitted from screen capture."""
    if sys.platform == "win32" and hwnd:
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x00000011)
        except Exception:
            log.exception("SetWindowDisplayAffinity failed")


def set_click_through(hwnd, enable):
    if sys.platform != "win32" or not hwnd:
        return
    GWL = -20
    LAY = 0x00080000
    TRN = 0x00000020
    s = ctypes.windll.user32.GetWindowLongW(hwnd, GWL)
    if enable:
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL, s | LAY | TRN)
    else:
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL, s & ~TRN)


# ----- Markdown inline parser (used by message renderer) ----------------------
def _parse_md_inline(line):
    """Split a line into (text, extra_tag_or_None) tokens for inline markdown."""
    out, pending = [], []

    def flush():
        if pending:
            out.append(("".join(pending), None))
            pending.clear()

    i, n = 0, len(line)
    while i < n:
        if line.startswith("**", i) and i + 2 < n and line[i + 2] != " ":
            end = line.find("**", i + 2)
            if end != -1 and line[end - 1] != " ":
                flush(); out.append((line[i + 2:end], "md_bold")); i = end + 2; continue
        if line[i] == "`":
            end = line.find("`", i + 1)
            if end != -1:
                flush(); out.append((line[i + 1:end], "md_code")); i = end + 1; continue
        if line[i] == "*" and i + 1 < n and line[i + 1] not in "* ":
            end = line.find("*", i + 1)
            if end != -1 and line[end - 1] != " " and (end + 1 >= n or line[end + 1] != "*"):
                flush(); out.append((line[i + 1:end], "md_italic")); i = end + 1; continue
        pending.append(line[i]); i += 1
    flush()
    return out


class CopyableChat(scrolledtext.ScrolledText):
    """Read-only Text widget that still allows Ctrl-C/A and arrow navigation."""
    def __init__(self, master, **kw):
        kw.setdefault("cursor", "arrow")
        super().__init__(master, **kw)
        self.bind("<Key>", self._block)
        self.bind("<Control-c>", self._copy)
        self.bind("<Control-C>", self._copy)
        self.bind("<Control-a>", self._sel_all)
        self.bind("<Control-A>", self._sel_all)

    def _block(self, e):
        if e.keysym in {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next"}:
            return None
        if e.state & 0x4 and e.keysym.lower() in ("c", "a", "x"):
            return None
        return "break"

    def _copy(self, e=None):
        try:
            t = self.get(tk.SEL_FIRST, tk.SEL_LAST)
            self.clipboard_clear(); self.clipboard_append(t)
        except tk.TclError:
            pass
        return "break"

    def _sel_all(self, e=None):
        self.tag_add(tk.SEL, "1.0", tk.END); return "break"


class InterviewAssistant:
    WIN_TITLE = "IA_Stealth_Overlay"

    def __init__(self):
        # Block startup until the mandatory Groq key is on file.
        self._ensure_groq_key()

        self.root = tk.Tk()
        self.root.title(self.WIN_TITLE)
        self.root.withdraw()
        self.root.geometry(f"{cfg.WINDOW_WIDTH}x{cfg.WINDOW_HEIGHT}+{cfg.WINDOW_X}+{cfg.WINDOW_Y}")
        self.root.configure(bg=cfg.BG)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", cfg.DEFAULT_OPACITY)
        self.root.overrideredirect(True)

        # ---- App state ----
        self.active_model_var = tk.StringVar(value=cfg.DEFAULT_MODEL)
        self.llm = None
        self._init_llm_service()

        self.session = SessionManager()
        self.screenshots = []        # list of base64 PNGs queued for next send
        self.ss_photos = []          # list of PhotoImages for thumbnail strip
        self._drag_x = 0; self._drag_y = 0
        self.is_thinking = False
        self.click_through = True
        self._ct_paused = False
        self.settings_open = False
        self._alpha = cfg.DEFAULT_OPACITY
        self._hidden = False
        self._hwnd = 0
        self._model_popup = None

        # Audio state
        self.audio_listener = None; self.audio_listening = False
        self.manual_recorder = None; self.manual_recording = False; self.manual_busy = False
        self.manual_btn_var = tk.StringVar(value="⏺ Rec")
        self.manual_btns = []
        self.llm_lock = threading.Lock()

        # ---- Build UI ----
        self._build_titlebar()
        self._build_body()
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.after(200, self._init_stealth)
        self._start_session()
        self._register_hotkeys()
        self.chat_entry.focus_set()
        self.root.mainloop()

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------
    def _ensure_groq_key(self):
        """Mandatory: prompt for the Groq key before the overlay is built."""
        key = get_api_key("groq")
        if key:
            return
        tmp = tk.Tk()
        tmp.withdraw()
        while True:
            entered = simpledialog.askstring(
                "Groq API Key Required",
                "Enter your Groq API key to continue.\n"
                "Groq powers the free default model and audio transcription.\n"
                "(Free key from console.groq.com)",
                parent=tmp,
            )
            if entered and entered.strip():
                keys = load_api_keys()
                keys["groq"] = entered.strip()
                save_api_keys(keys)
                tmp.destroy()
                return
            # User cancelled — confirm exit
            if not messagebox.askretrycancel(
                "Groq Key Required",
                "A Groq key is required to use Interview Assistant.\nRetry?",
                parent=tmp,
            ):
                tmp.destroy()
                sys.exit(0)

    def _init_llm_service(self):
        try:
            self.llm = LLMService(self.active_model_var.get())
            log.info("LLM initialised: %s", self.active_model_var.get())
        except Exception:
            self.llm = None
            log.exception("LLM init failed for model %s", self.active_model_var.get())

    def _init_stealth(self):
        self._hwnd = _win_hwnd(self.WIN_TITLE)
        apply_stealth(self._hwnd)
        if self.click_through:
            set_click_through(self._hwnd, True)
            log.info("Click-through enabled at startup (default ON)")

    # ------------------------------------------------------------------
    # Titlebar
    # ------------------------------------------------------------------
    def _build_titlebar(self):
        tb = tk.Frame(self.root, bg="#010409", height=34)
        tb.pack(fill="x"); tb.pack_propagate(False)
        tb.bind("<ButtonPress-1>", self._drag_start)
        tb.bind("<B1-Motion>", self._drag_motion)

        tk.Label(tb, text="⚡", bg="#010409", fg=cfg.ACCENT,
                 font=("Segoe UI", 12)).pack(side="left", padx=(8, 2))
        tk.Label(tb, text="Interview Assistant", bg="#010409", fg=cfg.FG,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        self.status_lbl = tk.Label(tb, text="● connecting", bg="#010409",
                                    fg=cfg.YELLOW, font=cfg.FONT_TINY)
        self.status_lbl.pack(side="left", padx=6)

        self._tb_btn(tb, "✕", self._quit, cfg.RED, side="right")
        self._tb_btn(tb, "⚙", self._toggle_settings, cfg.FG2, side="right")

        # Audio status indicator
        self.audio_status_var = tk.StringVar(value="🎙 Off")
        self.audio_status_lbl = tk.Label(tb, textvariable=self.audio_status_var,
                                          bg="#010409", fg=cfg.FG2,
                                          font=cfg.FONT_SML, padx=8)
        self.audio_status_lbl.pack(side="right", padx=4)

        # Custom in-window model selector — clicking opens a Frame placed
        # inside self.root (which inherits stealth), not a Toplevel.
        self.model_btn = tk.Label(tb, textvariable=self.active_model_var,
                                   bg="#010409", fg=cfg.ACCENT,
                                   font=("Segoe UI", 8), cursor="", padx=6)
        self.model_btn.pack(side="right", padx=4)
        self.model_btn.bind("<Button-1>", lambda e: self._toggle_model_popup())

    def _tb_btn(self, p, text, cmd, color, side="right"):
        l = tk.Label(p, text=text, bg="#010409", fg=color,
                     font=("Segoe UI", 11, "bold"), cursor="", padx=6, pady=2)
        l.pack(side=side); l.bind("<Button-1>", lambda e: cmd())
        l.bind("<Enter>", lambda e: l.config(fg=cfg.FG))
        l.bind("<Leave>", lambda e: l.config(fg=color))
        return l

    # ------------------------------------------------------------------
    # Model selector popup (Frame inside self.root => inherits stealth)
    # ------------------------------------------------------------------
    def _toggle_model_popup(self):
        if self._model_popup is not None:
            self._close_model_popup()
            return
        self._open_model_popup()

    def _open_model_popup(self):
        pnl = tk.Frame(self.root, bg=cfg.BG2,
                       highlightbackground=cfg.ACCENT, highlightthickness=1)
        self._model_popup = pnl

        models = available_models()
        if not models:
            tk.Label(pnl, text="No models available.\nAdd an API key in ⚙ Settings.",
                     bg=cfg.BG2, fg=cfg.FG2, font=cfg.FONT_SML,
                     padx=12, pady=8, justify="left").pack(fill="x")
        else:
            for m in models.keys():
                is_active = m == self.active_model_var.get()
                lbl = tk.Label(pnl, text=m,
                               bg=cfg.BG3 if is_active else cfg.BG2,
                               fg=cfg.ACCENT if is_active else cfg.FG,
                               font=("Segoe UI", 9), padx=12, pady=6,
                               anchor="w", cursor="")
                lbl.pack(fill="x")
                lbl.bind("<Enter>", lambda e, l=lbl: l.config(bg=cfg.BG3))
                lbl.bind("<Leave>", lambda e, l=lbl, active=is_active:
                         l.config(bg=cfg.BG3 if active else cfg.BG2))
                lbl.bind("<Button-1>", lambda e, v=m: self._select_model(v))

        pnl.place(relx=1.0, rely=0.0, anchor="ne", x=-2, y=36)
        self.root.bind("<Button-1>", self._on_root_click_dismiss, add="+")

    def _on_root_click_dismiss(self, e):
        if self._model_popup is None:
            return
        px = self._model_popup.winfo_rootx()
        py = self._model_popup.winfo_rooty()
        pw = self._model_popup.winfo_width()
        ph = self._model_popup.winfo_height()
        if not (px <= e.x_root <= px + pw and py <= e.y_root <= py + ph):
            self._close_model_popup()

    def _select_model(self, val):
        self._close_model_popup()
        self.active_model_var.set(val)
        self._init_llm_service()
        if not self.llm:
            self._append("ai",
                f"Could not switch to {val}. Check API key in ⚙ Settings.", "Error")

    def _close_model_popup(self):
        if self._model_popup is not None:
            self._model_popup.place_forget()
            self._model_popup.destroy()
            self._model_popup = None
        self.root.unbind("<Button-1>")

    # ------------------------------------------------------------------
    # Settings panel
    # ------------------------------------------------------------------
    def _toggle_settings(self):
        if self.settings_open:
            self._settings_frame.destroy(); self.settings_open = False
        else:
            self._build_settings_panel(); self.settings_open = True

    def _build_settings_panel(self):
        pnl = tk.Frame(self.root, bg=cfg.BG2,
                       highlightbackground=cfg.ACCENT, highlightthickness=1)
        pnl.place(relx=1.0, rely=0.0, anchor="ne", x=-2, y=36)
        self._settings_frame = pnl
        hdr = tk.Frame(pnl, bg=cfg.BG3); hdr.pack(fill="x")
        tk.Label(hdr, text="⚙  Settings & API Keys", bg=cfg.BG3, fg=cfg.ACCENT,
                 font=("Segoe UI", 9, "bold"), padx=10, pady=6).pack(side="left")
        cx = tk.Label(hdr, text="✕", bg=cfg.BG3, fg=cfg.FG2, cursor="",
                      font=("Segoe UI", 11), padx=8)
        cx.pack(side="right"); cx.bind("<Button-1>", lambda e: self._toggle_settings())
        tk.Frame(pnl, bg=cfg.BG3, height=1).pack(fill="x")

        keys_frame = tk.Frame(pnl, bg=cfg.BG2)
        keys_frame.pack(fill="x", padx=10, pady=8)

        saved_keys = load_api_keys()

        def _mask(key):
            if not key:
                return ""
            if len(key) <= 4:
                return key
            return "*" * (len(key) - 4) + key[-4:]

        def _make_key_row(parent, row, label, provider):
            real_key = saved_keys.get(provider, "") or ""
            tk.Label(parent, text=label, bg=cfg.BG2, fg=cfg.FG,
                     font=cfg.FONT_SML).grid(row=row, column=0, sticky="w", pady=2)
            entry = tk.Entry(parent, bg=cfg.BG3, fg=cfg.FG, width=25,
                             insertbackground=cfg.ACCENT, show="")
            entry.grid(row=row, column=1, padx=5, pady=2)
            entry.insert(0, _mask(real_key))
            entry.config(state="readonly")
            entry._real_key = real_key
            entry._editing = False

            def toggle_edit():
                if entry._editing:
                    new_key = entry.get().strip()
                    entry._real_key = new_key
                    entry.config(state="normal")
                    entry.delete(0, tk.END)
                    entry.insert(0, _mask(new_key))
                    entry.config(state="readonly")
                    btn.config(text="✏")
                    entry._editing = False
                else:
                    entry.config(state="normal")
                    entry.delete(0, tk.END)
                    entry.insert(0, entry._real_key)
                    btn.config(text="✔")
                    entry._editing = True
                    entry.focus_set()

            btn = tk.Label(parent, text="✏", bg=cfg.BG3, fg=cfg.ACCENT,
                           font=("Segoe UI", 10), cursor="", padx=4)
            btn.grid(row=row, column=2, pady=2)
            btn.bind("<Button-1>", lambda e: toggle_edit())
            return entry

        groq_entry = _make_key_row(keys_frame, 0, "Groq API Key:", "groq")
        openai_entry = _make_key_row(keys_frame, 1, "OpenAI API Key:", "openai")
        claude_entry = _make_key_row(keys_frame, 2, "Claude API Key:", "claude")

        def save_keys():
            def _get_real(entry):
                return entry.get().strip() if entry._editing else entry._real_key
            keys = {
                "groq": _get_real(groq_entry),
                "openai": _get_real(openai_entry),
                "claude": _get_real(claude_entry),
            }
            save_api_keys(keys)
            # Re-init LLM in case the active model's key was just added/changed.
            self._init_llm_service()
            # If active model lost its key, fall back to default (Groq).
            current = self.active_model_var.get()
            if current not in available_models():
                self.active_model_var.set(cfg.DEFAULT_MODEL)
                self._init_llm_service()
            self._toggle_settings()

        tk.Button(keys_frame, text="Save Keys", command=save_keys,
                  bg=cfg.ACCENT, fg=cfg.BG, font=cfg.FONT_SML,
                  relief="flat").grid(row=3, column=0, columnspan=3, pady=5)

        tk.Frame(pnl, bg=cfg.BG3, height=1).pack(fill="x", pady=(4, 0))

        # Opacity slider
        orow = tk.Frame(pnl, bg=cfg.BG2); orow.pack(fill="x", padx=10, pady=4)
        tk.Label(orow, text="Opacity:", bg=cfg.BG2, fg=cfg.FG2,
                 font=cfg.FONT_SML, width=10, anchor="w").pack(side="left")
        vl = tk.Label(orow, text=f"{int(self._alpha*100)}%",
                      bg=cfg.BG2, fg=cfg.FG, font=cfg.FONT_SML, width=4)
        vl.pack(side="right")

        def on_s(v):
            a = round(float(v), 2)
            self._alpha = a
            self.root.attributes("-alpha", a)
            vl.config(text=f"{int(a*100)}%")

        sl = tk.Scale(orow, from_=0.1, to=1.0, resolution=0.05, orient="horizontal",
                      command=on_s, bg=cfg.BG2, fg=cfg.FG, troughcolor=cfg.BG3,
                      highlightthickness=0, showvalue=False, sliderlength=14, length=150)
        sl.set(self._alpha); sl.pack(side="left", padx=4)

        tk.Frame(pnl, bg=cfg.BG3, height=1).pack(fill="x", pady=(4, 0))
        tk.Label(pnl, text="Keyboard Shortcuts", bg=cfg.BG2, fg=cfg.ACCENT,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=10, pady=(6, 2))
        for key, desc in [
            ("Alt+G", "Capture screenshot (attach)"),
            ("Alt+E", "Send"),
            ("Alt+Shift+A", "Auto audio listen"),
            ("Alt+Shift+D", "Manual record (toggle)"),
            ("Alt+T", "Toggle click-through"),
            ("Alt+P", "Focus prompt input"),
            ("Alt+L", "Clear chat"),
            ("Alt+H", "Hide / Un-hide"),
            ("Alt+Q", "Quit"),
            ("Alt+S", "Settings"),
            ("Alt+↑↓←→", "Move window"),
        ]:
            r = tk.Frame(pnl, bg=cfg.BG2); r.pack(fill="x", padx=10, pady=1)
            tk.Label(r, text=key, bg=cfg.BG3, fg=cfg.PURPLE,
                     font=("Consolas", 8), padx=4, pady=1).pack(side="left")
            tk.Label(r, text=f"  {desc}", bg=cfg.BG2, fg=cfg.FG2,
                     font=cfg.FONT_TINY).pack(side="left")
        tk.Frame(pnl, bg=cfg.BG2, height=8).pack()

    # ------------------------------------------------------------------
    # Body — unified chat surface
    # ------------------------------------------------------------------
    def _build_body(self):
        body = tk.Frame(self.root, bg=cfg.BG); body.pack(fill="both", expand=True)

        # Chat history
        self.chat_display = CopyableChat(body, wrap="word", bg=cfg.BG, fg=cfg.FG,
                                          font=cfg.FONT_MONO, relief="flat",
                                          padx=10, pady=6, state="disabled")
        self.chat_display.pack(fill="both", expand=True, padx=4, pady=(4, 2))
        self._config_tags(self.chat_display)

        # Attachment thumbnail strip — only shown when attachments exist.
        self.attach_frame = tk.Frame(body, bg=cfg.BG2)
        # Not packed yet; _refresh_thumbs() will pack/unpack as needed.

        # Input row
        inp = tk.Frame(body, bg=cfg.BG2); inp.pack(fill="x", padx=4, pady=(2, 2))
        self.chat_entry = tk.Text(inp, height=3, bg=cfg.BG3, fg=cfg.FG,
                                   insertbackground=cfg.ACCENT, font=cfg.FONT_MONO,
                                   relief="flat", padx=6, pady=4, wrap="word",
                                   cursor="arrow")
        self.chat_entry.pack(fill="x", padx=(4, 0), pady=4, side="left", expand=True)
        self.chat_entry.bind("<Return>", self._on_enter)
        self.chat_entry.bind("<Shift-Return>", lambda e: None)
        self._arrow_btn(inp, self._send_message).pack(side="right", padx=4, pady=4, fill="y")

        # Bottom action bar
        bar = tk.Frame(body, bg=cfg.BG); bar.pack(fill="x", padx=4, pady=(0, 4))
        self._pill(bar, "📸 Attach", self._capture_screenshot, cfg.GREEN).pack(side="left", padx=4)
        self.ss_count_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self.ss_count_var, bg=cfg.BG, fg=cfg.FG2,
                 font=cfg.FONT_SML).pack(side="left", padx=4)

        # Manual rec button on the right
        rec_btn = tk.Label(bar, textvariable=self.manual_btn_var, bg=cfg.GREEN,
                           fg=cfg.BG, font=("Segoe UI", 9, "bold"),
                           padx=8, pady=3, cursor="", relief="flat")
        rec_btn.pack(side="right", padx=4)
        rec_btn.bind("<Button-1>", lambda e: self._toggle_manual_recording())
        self.manual_btns.append(rec_btn)
        self._pill(bar, "🗑 Clear", self._clear_chat, cfg.BG3, cfg.FG2).pack(side="right", padx=4)

    def _arrow_btn(self, p, cmd):
        b = tk.Label(p, text="  ➤  ", bg=cfg.ACCENT, fg=cfg.BG,
                     font=("Segoe UI", 13, "bold"),
                     cursor="", padx=4, pady=4, relief="flat")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.config(bg=cfg.FG, fg=cfg.BG))
        b.bind("<Leave>", lambda e: b.config(bg=cfg.ACCENT, fg=cfg.BG))
        return b

    def _pill(self, p, text, cmd, bg_c, fg_c=None):
        fg_c = fg_c or cfg.BG
        b = tk.Label(p, text=text, bg=bg_c, fg=fg_c,
                     font=("Segoe UI", 9, "bold"), padx=8, pady=4,
                     cursor="", relief="flat")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.config(fg=bg_c, bg=fg_c))
        b.bind("<Leave>", lambda e: b.config(fg=fg_c, bg=bg_c))
        return b

    # ------------------------------------------------------------------
    # Attachment / screenshot strip
    # ------------------------------------------------------------------
    def _refresh_thumbs(self):
        for w in self.attach_frame.winfo_children():
            w.destroy()
        if not self.screenshots:
            self.attach_frame.pack_forget()
            self.ss_count_var.set("")
            return
        # Show the strip just above the input row.
        self.attach_frame.pack(fill="x", padx=4, pady=(0, 2), before=self.chat_entry.master)
        self.ss_count_var.set(f"📎 {len(self.screenshots)}/{cfg.MAX_SCREENSHOTS}")
        for idx, photo in enumerate(self.ss_photos):
            card = tk.Frame(self.attach_frame, bg=cfg.BG3)
            card.pack(side="left", padx=3, pady=3)
            tk.Label(card, image=photo, bg=cfg.BG3).pack()
            tk.Label(card, text=f"#{idx+1}", bg=cfg.BG3, fg=cfg.FG2,
                     font=cfg.FONT_TINY).pack()
            xb = tk.Label(card, text="✕", bg=cfg.RED, fg="white",
                          font=("Segoe UI", 7, "bold"), cursor="", padx=3, pady=1)
            xb.place(relx=1.0, rely=0.0, anchor="ne", x=-1, y=1)
            xb.bind("<Button-1>", lambda e, i=idx: self._remove_screenshot(i))

    def _remove_screenshot(self, idx):
        if 0 <= idx < len(self.screenshots):
            self.screenshots.pop(idx)
            self.ss_photos.pop(idx)
            self._refresh_thumbs()

    def _capture_screenshot(self):
        if len(self.screenshots) >= cfg.MAX_SCREENSHOTS:
            self._append("ai",
                f"⚠ Max {cfg.MAX_SCREENSHOTS} screenshots attached. Remove one first.",
                "System")
            return
        # Hide overlay briefly so we capture the screen underneath, not ourselves.
        self.root.withdraw()
        self.root.after(250, self._do_capture)

    def _do_capture(self):
        try:
            img = ImageGrab.grab()
            b64 = img_to_b64(img)
            self.screenshots.append(b64)
            thumb = img.copy(); thumb.thumbnail((118, 66))
            self.ss_photos.append(ImageTk.PhotoImage(thumb))
            self.root.deiconify()
            log.info("Screenshot attached (%d/%d)", len(self.screenshots), cfg.MAX_SCREENSHOTS)
            self.root.after(0, self._refresh_thumbs)
        except Exception as e:
            log.exception("Screenshot capture failed")
            self.root.deiconify()
            self.root.after(0, lambda e=e: self._append("ai", f"Capture error: {e}", "Error"))

    # ------------------------------------------------------------------
    # Sending — unified path. Text-only or text+vision based on attachments.
    # ------------------------------------------------------------------
    def _on_enter(self, e):
        if e.state & 0x1:        # Shift held — insert newline
            return None
        self._send_message(); return "break"

    def _send_message(self):
        if not self.session.session_id or self.is_thinking:
            return
        if not self.llm:
            self._append("ai",
                "LLM not initialised. Check API key in ⚙ Settings.", "Error")
            return
        msg = self.chat_entry.get("1.0", "end").strip()
        has_attachments = bool(self.screenshots)
        if not msg and not has_attachments:
            return
        self.chat_entry.delete("1.0", "end")
        self._restore_click_through()

        # Echo the user turn into the transcript.
        display_text = msg or "(no prompt)"
        attach_note = f"  📎 {len(self.screenshots)} screenshot{'s' if len(self.screenshots) != 1 else ''}" if has_attachments else ""
        self._append("user", f"{display_text}{attach_note}", "You")

        if has_attachments:
            shots = list(self.screenshots)
            self.screenshots.clear(); self.ss_photos.clear()
            self._refresh_thumbs()
            threading.Thread(target=self._do_vision_send, args=(shots, msg),
                             daemon=True, name="VisionSend").start()
        else:
            threading.Thread(target=self._do_text_send, args=(msg,),
                             daemon=True, name="TextSend").start()

    def _do_text_send(self, msg):
        self.is_thinking = True
        self.root.after(0, lambda: self._set_thinking(True))
        try:
            log.info("Text chat: %d chars", len(msg))
            with self.llm_lock:
                data = self.llm.chat(msg, self.session)
            ans = data["answer"]; qt = data["question_type"]; mem = data["memory_depth"]
            self.root.after(0, lambda: self._set_thinking(False))
            self.root.after(0, lambda: self._append("ai", ans, f"AI [{qt}] mem:{mem}/5"))
        except Exception as e:
            log.exception("Text send failed")
            self.root.after(0, lambda: self._set_thinking(False))
            self.root.after(0, lambda e=e: self._append("ai", f"Error: {e}", "Error"))
        finally:
            self.is_thinking = False

    def _do_vision_send(self, shots, prompt):
        self.is_thinking = True
        self.root.after(0, lambda: self._set_thinking(True))
        try:
            log.info("Vision send: %d shots, prompt=%r", len(shots), prompt)
            with self.llm_lock:
                data = self.llm.analyze_screenshots(shots, prompt or None, self.session)
            ans = data["answer"]; qt = data["question_type"]; mem = data["memory_depth"]
            shots_n = data["screenshots_analyzed"]
            self.root.after(0, lambda: self._set_thinking(False))
            self.root.after(0, lambda: self._append(
                "ai", ans, f"AI [{qt}] │ {shots_n} shot(s) │ mem:{mem}/5"))
        except Exception as e:
            log.exception("Vision send failed")
            self.root.after(0, lambda: self._set_thinking(False))
            self.root.after(0, lambda e=e: self._append("ai", f"Error: {e}", "Error"))
        finally:
            self.is_thinking = False

    # ------------------------------------------------------------------
    # Display widget plumbing
    # ------------------------------------------------------------------
    def _config_tags(self, w):
        w.tag_configure("user_tag", background=cfg.USER_BG, foreground=cfg.FG,
                        font=("Segoe UI", 10, "bold"),
                        lmargin1=8, lmargin2=8, rmargin=8, spacing1=4)
        w.tag_configure("ai_tag", background=cfg.AI_BG, foreground=cfg.FG,
                        font=cfg.FONT_MONO,
                        lmargin1=8, lmargin2=8, rmargin=8, spacing3=4)
        w.tag_configure("interviewer_tag", background="#3a2a5c", foreground=cfg.FG,
                        font=("Segoe UI", 10, "bold"),
                        lmargin1=8, lmargin2=8, rmargin=8, spacing1=4)
        w.tag_configure("time_tag", foreground=cfg.FG2, font=("Segoe UI", 7))
        w.tag_configure("thinking", foreground=cfg.YELLOW,
                        font=("Segoe UI", 9, "italic"))
        # Markdown overrides — configured after ai_tag so they win on conflicts.
        w.tag_configure("md_bold", font=("Consolas", 10, "bold"))
        w.tag_configure("md_italic", font=("Consolas", 10, "italic"))
        w.tag_configure("md_code", background="#2a2a3a", font=("Consolas", 9))
        w.tag_configure("md_codeblock", background="#1c1c2c", font=("Consolas", 9),
                        lmargin1=16, lmargin2=16, spacing1=2, spacing3=2)
        w.tag_configure("md_header", foreground=cfg.ACCENT,
                        font=("Consolas", 11, "bold"), spacing1=4, spacing3=2)

    def _append(self, role, text, label=""):
        w = self.chat_display
        w.config(state="normal")
        ts = datetime.now().strftime("%H:%M")
        if role == "user":
            w.insert("end", f"\n{label}  [{ts}]\n", "user_tag")
            w.insert("end", f"{text}\n", "user_tag")
        elif role == "interviewer":
            w.insert("end", f"\n{label}  [{ts}]\n", "interviewer_tag")
            w.insert("end", f"{text}\n", "interviewer_tag")
        else:
            w.insert("end", f"\n{label}  [{ts}]\n", "time_tag")
            self._insert_markdown(w, text, "ai_tag")
        w.insert("end", "\n")
        w.config(state="disabled"); w.see("end")

    def _insert_markdown(self, w, text, base_tag):
        in_code = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code; continue
            if in_code:
                w.insert("end", line + "\n", (base_tag, "md_codeblock"))
                continue
            mh = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if mh:
                w.insert("end", mh.group(2) + "\n", (base_tag, "md_header"))
                continue
            mb = re.match(r"^(\s*)([-*])\s+(.*)$", line)
            if mb:
                indent, _, content = mb.groups()
                w.insert("end", f"{indent}• ", (base_tag,))
                for seg, extra in _parse_md_inline(content):
                    w.insert("end", seg, (base_tag,) if extra is None else (base_tag, extra))
                w.insert("end", "\n", (base_tag,)); continue
            mn = re.match(r"^(\s*)(\d+\.)\s+(.*)$", line)
            if mn:
                indent, num, content = mn.groups()
                w.insert("end", f"{indent}{num} ", (base_tag,))
                for seg, extra in _parse_md_inline(content):
                    w.insert("end", seg, (base_tag,) if extra is None else (base_tag, extra))
                w.insert("end", "\n", (base_tag,)); continue
            for seg, extra in _parse_md_inline(line):
                w.insert("end", seg, (base_tag,) if extra is None else (base_tag, extra))
            w.insert("end", "\n", (base_tag,))

    def _set_thinking(self, on):
        w = self.chat_display
        w.config(state="normal")
        if on:
            w.insert("end", "\n⏳ Thinking…\n\n", "thinking")
        else:
            c = w.get("1.0", "end"); m = "\n⏳ Thinking…\n\n"; p = c.rfind(m)
            if p != -1:
                w.delete(f"1.0 + {p} chars", f"1.0 + {p+len(m)} chars")
        w.config(state="disabled"); w.see("end")

    def _set_status(self, text, color=None):
        self.status_lbl.config(text=text, fg=color or cfg.GREEN)

    def _clear_chat(self):
        """Reset both the display widget and the session memory."""
        self.session.reset_session()
        w = self.chat_display
        w.config(state="normal"); w.delete("1.0", "end"); w.config(state="disabled")
        self.screenshots.clear(); self.ss_photos.clear(); self._refresh_thumbs()
        self._append("ai", "🔄 Chat cleared — new session.", "System")

    # ------------------------------------------------------------------
    # Session bootstrap
    # ------------------------------------------------------------------
    def _start_session(self):
        def _do():
            try:
                sid = self.session.start_session()
                log.info("Session started: %s", sid)
                self.root.after(0, lambda: self._set_status(f"● {sid[:8]}", cfg.GREEN))
                self.root.after(0, lambda: self._append(
                    "ai",
                    "👋 Hi! I'm your interview assistant.\n"
                    "Click-through is ON by default — clicks pass through to the app underneath.\n\n"
                    "Alt+P → Focus prompt   │  Alt+E → Send\n"
                    "Alt+G → Attach screenshot   │  Alt+L → Clear chat\n"
                    "Alt+Shift+A → Auto audio   │  Alt+Shift+D → Manual record\n"
                    "Alt+T → Toggle click-through   │  Alt+S → Settings\n\n"
                    "Tip: attach a screenshot to ask about what's on screen.",
                    "System"))
            except Exception:
                log.exception("Session start failed")
                self.root.after(0, lambda: self._set_status("● offline", cfg.RED))
        threading.Thread(target=_do, daemon=True).start()

    # ------------------------------------------------------------------
    # Stealth toggles
    # ------------------------------------------------------------------
    def _toggle_click_through(self):
        self.click_through = not self.click_through
        set_click_through(self._hwnd, self.click_through)
        msg = "Click-through ON" if self.click_through else "Click-through OFF"
        self._set_status(f"● {msg}", cfg.RED if self.click_through else cfg.GREEN)

    def _focus_prompt(self):
        if self._hidden:
            self.root.deiconify(); self._hidden = False
        if self.click_through:
            set_click_through(self._hwnd, False)
            self._ct_paused = True
        self.root.deiconify(); self.root.lift(); self.root.focus_force()
        self.chat_entry.focus_set()

    def _restore_click_through(self):
        if self._ct_paused and self.click_through:
            set_click_through(self._hwnd, True)
        self._ct_paused = False

    # ------------------------------------------------------------------
    # Audio
    # ------------------------------------------------------------------
    def _set_audio_status(self, text, color=None):
        self.audio_status_var.set(text)
        if color is not None:
            self.audio_status_lbl.config(fg=color)

    def _toggle_audio_mode(self):
        if self.audio_listening:
            self._stop_audio_listening(user_initiated=True)
        else:
            self._start_audio_listening()

    def _start_audio_listening(self):
        if self.audio_listening:
            return
        log.info("User requested audio listen ON")
        if AudioListener is None:
            self._append("ai",
                f"Audio capture unavailable: {AUDIO_IMPORT_ERROR}\nInstall: pip install soundcard numpy",
                "Error"); return
        if self.manual_recording or self.manual_busy:
            self._append("ai",
                "Stop manual recording (Alt+Shift+D) before starting auto mode.",
                "System"); return
        if not get_api_key("groq"):
            self._append("ai",
                "Groq API key required for audio transcription. Add it in ⚙ Settings.",
                "Error"); return
        try:
            self.audio_listener = AudioListener(
                transcribe_fn=transcribe_audio,
                on_transcript=lambda t: self.root.after(0, self._on_audio_transcript, t),
                on_status=lambda m: self.root.after(0, self._on_audio_status, m),
                on_segment_start=lambda: self.root.after(0, self._set_audio_status, "🎙 Capturing…", cfg.YELLOW),
                on_segment_end=lambda: self.root.after(0, self._set_audio_status, "🎙 Transcribing…", cfg.ACCENT),
            )
            self.audio_listener.start()
        except AudioCaptureError as e:
            log.error("AudioCaptureError on start: %s", e)
            self.audio_listener = None
            self._append("ai", f"Could not start audio capture: {e}", "Error")
            self._set_audio_status("🎙 Off", cfg.FG2); return
        except Exception as e:
            log.exception("Unexpected error starting audio listener")
            self.audio_listener = None
            self._append("ai", f"Audio error: {e}", "Error")
            self._set_audio_status("🎙 Off", cfg.FG2); return
        self.audio_listening = True
        self._set_audio_status("🎙 Listening", cfg.GREEN)
        hint = " — attach screenshots anytime; they'll be sent with the next transcript." \
            if AudioListener is not None else ""
        self._append("ai",
            f"🎙 Audio listening ON — capturing system audio.{hint}\n"
            "Press Alt+Shift+A again to stop.",
            "System")

    def _stop_audio_listening(self, user_initiated=False):
        if self.audio_listener is not None:
            try: self.audio_listener.stop()
            except Exception: log.exception("AudioListener.stop() raised")
            self.audio_listener = None
        if self.audio_listening:
            self.audio_listening = False
            self._set_audio_status("🎙 Off", cfg.FG2)
            if user_initiated:
                self._append("ai", "🎙 Audio listening OFF.", "System")

    def _on_audio_status(self, msg):
        log.warning("Audio status: %s", msg)
        self._append("ai", msg, "Audio")
        if self.audio_listening:
            self._set_audio_status("🎙 Listening", cfg.GREEN)

    def _on_audio_transcript(self, text):
        if not text:
            if self.audio_listening:
                self._set_audio_status("🎙 Listening", cfg.GREEN)
            return
        log.info("Interviewer transcript received (%d chars)", len(text))
        threading.Thread(target=self._dispatch_auto_transcript, args=(text,),
                         daemon=True, name="AudioReply").start()

    def _dispatch_auto_transcript(self, text):
        try:
            self._run_transcript_llm(text, label_prefix="auto")
        finally:
            if self.audio_listening:
                self.root.after(0, lambda: self._set_audio_status("🎙 Listening", cfg.GREEN))

    def _run_transcript_llm(self, transcript, label_prefix):
        """Shared dispatcher: post the transcript, decide text vs vision."""
        if not self.llm:
            self.root.after(0, lambda: self._append(
                "ai", "LLM not initialised. Check API key in ⚙ Settings.", "Error"))
            return
        shots = list(self.screenshots)
        use_vision = bool(shots)
        n_shots = len(shots)

        label = f"🎤 Interviewer ({label_prefix}"
        if use_vision:
            label += f", +{n_shots} screenshot{'s' if n_shots != 1 else ''}"
        label += ")"
        self.root.after(0, lambda: self._append("interviewer", transcript, label))

        # If we're sending screenshots with the transcript, clear the attach
        # tray — same UX rule as a typed message.
        if use_vision:
            self.screenshots.clear(); self.ss_photos.clear()
            self.root.after(0, self._refresh_thumbs)

        self.root.after(0, lambda: self._set_thinking(True))
        self.root.after(0, lambda: self._set_audio_status("🎙 Generating…", cfg.ACCENT))
        try:
            log.info("Transcript→LLM (%d chars, vision=%s, shots=%d)",
                     len(transcript), use_vision, n_shots)
            with self.llm_lock:
                if use_vision:
                    data = self.llm.analyze_screenshots(shots, transcript, self.session)
                    suffix = f"AI [{data['question_type']}] │ {n_shots} shot(s) │ mem:{data['memory_depth']}/5"
                else:
                    data = self.llm.chat(transcript, self.session)
                    suffix = f"AI [{data['question_type']}] mem:{data['memory_depth']}/5"
            ans = data["answer"]
            self.root.after(0, lambda: self._set_thinking(False))
            self.root.after(0, lambda: self._append("ai", ans, suffix))
        except Exception as e:
            log.exception("Transcript LLM call failed")
            self.root.after(0, lambda: self._set_thinking(False))
            self.root.after(0, lambda e=e: self._append("ai", f"Error: {e}", "Error"))

    # ------------------------------------------------------------------
    # Manual recording
    # ------------------------------------------------------------------
    def _set_manual_btn(self, text, bg):
        self.manual_btn_var.set(text)
        for btn in self.manual_btns:
            try: btn.config(bg=bg)
            except Exception: pass

    def _toggle_manual_recording(self):
        if self.manual_busy:
            log.info("Manual toggle ignored — finishing previous recording")
            return
        if self.manual_recording:
            self._stop_manual_recording()
        else:
            self._start_manual_recording()

    def _start_manual_recording(self):
        if self.manual_recording or self.manual_busy:
            return
        if ManualRecorder is None:
            self._append("ai",
                f"Audio capture unavailable: {AUDIO_IMPORT_ERROR}\nInstall: pip install soundcard numpy",
                "Error"); return
        if self.audio_listening:
            self._append("ai",
                "Stop auto mode (Alt+Shift+A) before starting manual recording.",
                "System"); return
        if not get_api_key("groq"):
            self._append("ai",
                "Groq API key required for transcription. Add it in ⚙ Settings.",
                "Error"); return
        try:
            self.manual_recorder = ManualRecorder(
                on_progress=lambda secs: self.root.after(0, self._on_manual_progress, secs),
                on_status=lambda m: self.root.after(0, self._on_audio_status, m),
            )
            self.manual_recorder.start()
        except AudioCaptureError as e:
            log.error("ManualRecorder failed to start: %s", e)
            self.manual_recorder = None
            self._append("ai", f"Could not start recording: {e}", "Error"); return
        except Exception as e:
            log.exception("Unexpected manual recorder error")
            self.manual_recorder = None
            self._append("ai", f"Recording error: {e}", "Error"); return
        self.manual_recording = True
        self._set_manual_btn("■ Stop", cfg.RED)
        self._set_audio_status("🔴 Recording 0s", cfg.RED)
        log.info("Manual recording started")
        self._append("ai",
            "🔴 Manual recording started — press Alt+Shift+D (or the Stop button) to finish.\n"
            "💡 Attach screenshots during recording — they'll be sent with the transcript.",
            "System")

    def _on_manual_progress(self, secs):
        if not self.manual_recording:
            return
        self._set_audio_status(f"🔴 Recording {int(secs)}s / {cfg.MANUAL_RECORD_MAX_S}s", cfg.RED)
        if secs >= cfg.MANUAL_RECORD_MAX_S:
            log.info("Manual recording reached %ds — auto-stopping", cfg.MANUAL_RECORD_MAX_S)
            self._append("ai",
                f"⏱ Reached {cfg.MANUAL_RECORD_MAX_S}s limit — stopping automatically.",
                "System")
            self._stop_manual_recording()

    def _stop_manual_recording(self):
        if not self.manual_recording or self.manual_busy:
            return
        log.info("User requested manual recording OFF")
        self.manual_recording = False
        self.manual_busy = True
        self._set_manual_btn("⏺ Rec", cfg.BG3)
        self._set_audio_status("🎙 Processing…", cfg.YELLOW)
        threading.Thread(target=self._finish_manual_recording,
                         daemon=True, name="ManualFinish").start()

    def _finish_manual_recording(self):
        recorder = self.manual_recorder
        self.manual_recorder = None
        if recorder is None:
            log.warning("Manual finish called with no recorder")
            self.root.after(0, self._reset_manual_state); return
        try:
            audio_i16, sr = recorder.stop()
        except Exception as e:
            log.exception("ManualRecorder.stop() raised")
            self.root.after(0, lambda e=e: self._append("ai", f"Stop error: {e}", "Error"))
            self.root.after(0, self._reset_manual_state); return
        if audio_i16 is None or audio_i16.size == 0:
            log.info("Manual recording produced no audio")
            self.root.after(0, lambda: self._append("ai", "No audio captured.", "System"))
            self.root.after(0, self._reset_manual_state); return

        duration = audio_i16.size / float(sr)
        log.info("Manual recording: %.2fs captured", duration)
        self.root.after(0, lambda: self._append(
            "ai", f"⏺ Recorded {duration:.1f}s — chunking and transcribing…", "System"))

        try:
            chunks = chunk_wav_for_whisper(audio_i16, sr, cfg.WHISPER_MAX_BYTES)
        except Exception as e:
            log.exception("Chunking failed")
            self.root.after(0, lambda e=e: self._append("ai", f"Chunk error: {e}", "Error"))
            self.root.after(0, self._reset_manual_state); return

        n = len(chunks)
        transcripts = [""] * n
        done_count = [0]
        done_lock = threading.Lock()
        self.root.after(0, lambda n=n: self._set_audio_status(f"🎙 Transcribing 0/{n}", cfg.ACCENT))

        def _do_chunk(i, wav):
            log.info("Transcribing chunk %d/%d (%d bytes)", i + 1, n, len(wav))
            try:
                text = transcribe_audio(wav)
            except Exception as e:
                log.exception("Chunk %d transcription failed", i + 1)
                self.root.after(0, lambda i=i, e=e: self._append(
                    "ai", f"Chunk {i+1}/{n} transcription failed: {e}", "Error"))
                return i, ""
            return i, (text or "")

        with ThreadPoolExecutor(max_workers=min(n, 4)) as pool:
            futures = [pool.submit(_do_chunk, i, wav) for i, wav in enumerate(chunks)]
            for fut in as_completed(futures):
                try:
                    idx, text = fut.result()
                except Exception:
                    log.exception("Chunk future failed"); continue
                transcripts[idx] = text
                with done_lock:
                    done_count[0] += 1
                    progress = done_count[0]
                self.root.after(0, lambda p=progress, n=n: self._set_audio_status(
                    f"🎙 Transcribing {p}/{n}", cfg.ACCENT))

        merged = " ".join(t for t in transcripts if t).strip()
        if not merged:
            log.info("Manual recording: no speech detected")
            self.root.after(0, lambda: self._append("ai", "No speech detected in recording.", "System"))
            self.root.after(0, self._reset_manual_state); return

        log.info("Merged transcript: %d chars from %d chunks", len(merged), n)
        try:
            self._run_transcript_llm(merged,
                label_prefix=f"manual, {n} chunk{'s' if n != 1 else ''}, {duration:.1f}s")
        finally:
            self.root.after(0, self._reset_manual_state)

    def _reset_manual_state(self):
        self.manual_busy = False
        self._set_manual_btn("⏺ Rec", cfg.GREEN)
        if not self.audio_listening:
            self._set_audio_status("🎙 Off", cfg.FG2)

    # ------------------------------------------------------------------
    # Window movement / hide / quit
    # ------------------------------------------------------------------
    def _toggle_hide(self):
        if self._hidden:
            self.root.deiconify(); self.root.after(100, self._init_stealth); self._hidden = False
        else:
            self.root.withdraw(); self._hidden = True

    def _move_window(self, dx, dy):
        x = self.root.winfo_x() + dx; y = self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")

    def _register_hotkeys(self):
        self.hotkeys = HotkeyManager()
        atexit.register(self.hotkeys.stop)

        def hk(c, f):
            self.hotkeys.add(c, lambda: self.root.after(0, f))

        hk("alt+g", self._capture_screenshot)
        hk("alt+e", self._send_message)
        hk("alt+t", self._toggle_click_through)
        hk("alt+p", self._focus_prompt)
        hk("alt+l", self._clear_chat)
        hk("alt+h", self._toggle_hide)
        hk("alt+q", self._quit)
        hk("alt+s", self._toggle_settings)
        hk("alt+shift+a", self._toggle_audio_mode)
        hk("alt+shift+d", self._toggle_manual_recording)
        hk("alt+up", lambda: self._move_window(0, -cfg.MOVE_STEP))
        hk("alt+down", lambda: self._move_window(0, cfg.MOVE_STEP))
        hk("alt+left", lambda: self._move_window(-cfg.MOVE_STEP, 0))
        hk("alt+right", lambda: self._move_window(cfg.MOVE_STEP, 0))
        self.hotkeys.start()

    def _drag_start(self, e):
        self._drag_x = e.x; self._drag_y = e.y

    def _drag_motion(self, e):
        x = self.root.winfo_x() + (e.x - self._drag_x)
        y = self.root.winfo_y() + (e.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _quit(self):
        log.info("Quit requested")
        self._stop_audio_listening(user_initiated=False)
        if self.manual_recorder is not None:
            try: self.manual_recorder.stop()
            except Exception: log.exception("ManualRecorder.stop() raised on quit")
            self.manual_recorder = None
        try: self.hotkeys.stop()
        except Exception: log.exception("HotkeyManager.stop() raised on quit")
        self.root.destroy()


# Entry point lives in main.py — this module only defines the app class.
