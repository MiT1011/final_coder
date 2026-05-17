"""Pre-launch splash with a Start button + progress bar.

Why this exists: the heavy SDKs (openai, anthropic, pydantic-core, numpy,
soundcard) take 1–3s to import even on a fast machine, and PyInstaller's
onefile bootloader needs extra time to extract the bundle. Without this
splash, the user double-clicks the .exe and stares at nothing for 5–15s,
then briefly sees a login window. With this splash:

1. The Start button appears as soon as Python is up.
2. Clicking Start triggers a background thread that warms each heavy module.
3. The progress bar advances as each module loads.
4. Once everything is in `sys.modules`, the splash closes and the real app
   (login -> main) takes over — its re-imports are cache hits, basically
   free.

The splash itself imports only `tkinter` + stdlib so it can show instantly.
"""
import tkinter as tk
from tkinter import ttk
import logging
import importlib

log = logging.getLogger(__name__)


class SplashWindow:
    # Heavy modules to warm up. Order is light -> heavy so the bar advances
    # smoothly even though step durations differ wildly.
    _LOAD_STEPS = [
        ("Initialising core...",      "config"),
        ("Loading hotkeys...",        "win_hotkeys"),
        ("Loading authentication...", "auth"),
        ("Loading LLM providers...",  "llm_service"),
        ("Loading audio engine...",   "audio_service"),
        ("Loading main UI...",        "app"),
    ]

    def __init__(self):
        self.proceed = False
        self.root = tk.Tk()
        self.root.title("Interview Assistant")
        self.root.geometry("420x320")
        self.root.configure(bg="#0d1117")
        self.root.resizable(False, False)
        self._center()

        tk.Label(self.root, text="⚡", font=("Segoe UI", 56),
                 bg="#0d1117", fg="#58a6ff").pack(pady=(35, 5))
        tk.Label(self.root, text="Interview Assistant",
                 font=("Segoe UI", 16, "bold"),
                 bg="#0d1117", fg="#e6edf3").pack()
        tk.Label(self.root, text="Stealth chat overlay",
                 font=("Segoe UI", 9),
                 bg="#0d1117", fg="#8b949e").pack(pady=(0, 25))

        self.start_btn = tk.Button(self.root, text="Start",
                                    command=self._on_start,
                                    bg="#58a6ff", fg="#0d1117",
                                    font=("Segoe UI", 11, "bold"),
                                    relief="flat", padx=40, pady=8,
                                    cursor="hand2",
                                    activebackground="#79b8ff",
                                    activeforeground="#0d1117")
        self.start_btn.pack(pady=10)

        self.progress_frame = tk.Frame(self.root, bg="#0d1117")
        # ttk Progressbar honours theme colors only via ttk.Style; pick a
        # theme that actually paints the background colour we set.
        style = ttk.Style()
        try:
            style.theme_use("clam")
            style.configure("IA.Horizontal.TProgressbar",
                            troughcolor="#161b22",
                            background="#58a6ff",
                            bordercolor="#161b22",
                            lightcolor="#58a6ff",
                            darkcolor="#58a6ff",
                            thickness=10)
        except Exception:
            log.exception("Splash: ttk style setup failed (using defaults)")
        self.progress_bar = ttk.Progressbar(self.progress_frame, length=320,
                                            mode="determinate", maximum=100,
                                            style="IA.Horizontal.TProgressbar")
        self.progress_bar.pack(pady=(15, 5))
        self.progress_status = tk.Label(self.progress_frame, text="",
                                         bg="#0d1117", fg="#8b949e",
                                         font=("Segoe UI", 8))
        self.progress_status.pack()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_start(self):
        self.start_btn.pack_forget()
        self.progress_frame.pack(pady=10)
        self.root.update_idletasks()
        # Run the loader on the MAIN Tk thread (not a worker). soundcard +
        # several Windows-API-bound deps initialise per-thread state on first
        # import — if we import them on a background thread, the actual
        # audio capture (which runs on its own thread later) ends up with no
        # COM/WASAPI init in scope and silently returns empty buffers.
        # update_idletasks() between steps keeps the progress bar painting.
        self.root.after(50, self._load_next_step, 0)

    def _load_next_step(self, idx):
        steps = self._LOAD_STEPS
        n = len(steps)
        if idx >= n:
            self._set_progress(100, "Ready")
            self.root.update_idletasks()
            self.proceed = True
            self.root.after(400, self.root.destroy)
            return
        label, mod = steps[idx]
        pct = int((idx / n) * 100)
        self._set_progress(pct, label)
        self.root.update_idletasks()
        try:
            importlib.import_module(mod)
        except Exception:
            # Log and continue — the real importer in main.py / app.py
            # will surface a hard error if the module is genuinely broken.
            log.exception("Splash loader: import %s failed", mod)
        # Yield to the event loop so the bar repaints before the next
        # (potentially slow) import blocks the thread again.
        self.root.after(10, self._load_next_step, idx + 1)

    def _set_progress(self, pct, label):
        self.progress_bar["value"] = pct
        self.progress_status.config(text=label)

    def _on_close(self):
        self.proceed = False
        self.root.destroy()

    def _center(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws - w) // 2
        y = (hs - h) // 2
        self.root.geometry(f"+{x}+{y}")

    def run(self):
        self.root.mainloop()
        return self.proceed
