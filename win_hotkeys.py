"""Global hotkeys backed by Windows `RegisterHotKey`.

The `keyboard` library's `suppress=True` mode is racy for Alt+letter combos —
the low-level hook can be slow enough that the key leaks to the foreground
window (VS Code's Edit menu opens, Chrome navigates history, etc.).

`RegisterHotKey` is the canonical Windows API for this: it reserves the combo
at the OS level. Once we hold a hotkey, no other process — including the
focused app — sees the keystroke. This is how OBS, Spotify, Steam Overlay,
NVIDIA GeForce Experience, etc. all do it.

Design:
- One dedicated daemon thread runs the message pump (`GetMessageW`).
- `RegisterHotKey` is called from that same thread (required: the WM_HOTKEY
  message is delivered to the thread that registered).
- Callers `.add()` combos *before* `.start()`. We don't support late
  registration since the registration must happen from the pump thread.
- `.stop()` posts WM_QUIT to the pump thread and joins it briefly.

This module is Windows-only. Non-Windows builds should not import it.
"""
import ctypes
import ctypes.wintypes as wt
import threading
import logging

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Modifier bitmasks for RegisterHotKey
MOD_ALT      = 0x0001
MOD_CONTROL  = 0x0002
MOD_SHIFT    = 0x0004
MOD_WIN      = 0x0008
MOD_NOREPEAT = 0x4000  # Don't fire repeatedly while the key is held

WM_HOTKEY = 0x0312
WM_QUIT   = 0x0012

# Virtual-key codes we care about
VK_BACK   = 0x08; VK_TAB    = 0x09; VK_RETURN = 0x0D
VK_ESCAPE = 0x1B; VK_SPACE  = 0x20
VK_LEFT   = 0x25; VK_UP     = 0x26; VK_RIGHT  = 0x27; VK_DOWN = 0x28
VK_F1     = 0x70  # F1..F24 are sequential from here

_SPECIAL_KEYS = {
    "left": VK_LEFT, "right": VK_RIGHT, "up": VK_UP, "down": VK_DOWN,
    "tab": VK_TAB, "enter": VK_RETURN, "return": VK_RETURN,
    "escape": VK_ESCAPE, "esc": VK_ESCAPE, "space": VK_SPACE,
    "backspace": VK_BACK,
}

# Explicit ctypes signatures so 32/64-bit doesn't bite us
user32.RegisterHotKey.argtypes   = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype    = wt.BOOL
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype  = wt.BOOL
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, ctypes.c_void_p, ctypes.c_void_p]
user32.PostThreadMessageW.restype  = wt.BOOL
user32.GetMessageW.argtypes   = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype    = ctypes.c_int           # signed: returns -1 on error
user32.PeekMessageW.argtypes  = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.PeekMessageW.restype   = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
kernel32.GetCurrentThreadId.restype = wt.DWORD


def _parse_combo(combo):
    """'alt+shift+a' -> (modifiers_bitmask, virtual_key_code)."""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    mods = 0
    vk = None
    for p in parts:
        if   p == "alt":                       mods |= MOD_ALT
        elif p in ("ctrl", "control"):         mods |= MOD_CONTROL
        elif p == "shift":                     mods |= MOD_SHIFT
        elif p in ("win", "super", "meta"):    mods |= MOD_WIN
        elif p in _SPECIAL_KEYS:               vk = _SPECIAL_KEYS[p]
        elif p.startswith("f") and p[1:].isdigit():
            n = int(p[1:])
            if 1 <= n <= 24:
                vk = VK_F1 + (n - 1)
            else:
                raise ValueError(f"Function key out of range: {p!r}")
        elif len(p) == 1 and (p.isalpha() or p.isdigit()):
            vk = ord(p.upper())
        else:
            raise ValueError(f"Unsupported key token: {p!r} in combo {combo!r}")
    if vk is None:
        raise ValueError(f"Combo missing main key: {combo!r}")
    return mods, vk


class HotkeyManager:
    """Reserve a set of global hotkeys at the OS level. Once `.start()` is
    called, the registered combos no longer reach any other application.

    Usage:
        mgr = HotkeyManager()
        mgr.add("alt+shift+s", on_screenshot_mode)
        mgr.add("alt+e",       on_submit)
        mgr.start()
        ...
        mgr.stop()   # releases all hotkeys
    """
    def __init__(self):
        self._pending = []                     # (combo, callback) before start()
        self._id_to_cb = {}                    # registered id -> callback
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()

    def add(self, combo, callback):
        if self._thread is not None:
            raise RuntimeError("HotkeyManager.add() called after start()")
        self._pending.append((combo, callback))

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="HotkeyPump", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            log.warning("HotkeyManager start timed out — hotkeys may not be active")

    def stop(self):
        if self._thread is None or self._thread_id is None:
            return
        log.info("HotkeyManager stopping (thread_id=%d)", self._thread_id)
        if not user32.PostThreadMessageW(self._thread_id, WM_QUIT, None, None):
            err = kernel32.GetLastError()
            log.warning("PostThreadMessage(WM_QUIT) failed: WinErr=%d", err)
        self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = None

    # ----- internals -----
    def _loop(self):
        self._thread_id = kernel32.GetCurrentThreadId()
        log.info("HotkeyPump thread started (id=%d)", self._thread_id)

        # Force the OS to materialise this thread's message queue so the
        # subsequent RegisterHotKey calls have a queue to target.
        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # PM_NOREMOVE

        # Register everything queued via .add()
        next_id = 1
        for combo, cb in self._pending:
            try:
                mods, vk = _parse_combo(combo)
            except Exception:
                log.exception("Hotkey parse failed: %r", combo)
                continue
            ok = user32.RegisterHotKey(None, next_id, mods | MOD_NOREPEAT, vk)
            if not ok:
                err = kernel32.GetLastError()
                log.error("RegisterHotKey failed for %s (mods=0x%x vk=0x%x): WinErr=%d "
                          "(1409 = hotkey already taken by another app)",
                          combo, mods, vk, err)
                continue
            self._id_to_cb[next_id] = cb
            log.info("Registered hotkey %-18s -> id=%d (mods=0x%x vk=0x%x)",
                     combo, next_id, mods, vk)
            next_id += 1
        self._pending = []
        self._ready.set()

        # Pump messages until WM_QUIT
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0:
                log.info("HotkeyPump received WM_QUIT")
                break
            if ret == -1:
                err = kernel32.GetLastError()
                log.error("GetMessageW returned -1, WinErr=%d", err)
                break
            if msg.message == WM_HOTKEY:
                hk_id = int(msg.wParam)
                cb = self._id_to_cb.get(hk_id)
                if cb is None:
                    log.warning("Unknown hotkey id fired: %d", hk_id)
                    continue
                try:
                    cb()
                except Exception:
                    log.exception("Hotkey callback raised (id=%d)", hk_id)
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

        # Unregister all on shutdown so other apps get their shortcuts back
        for hk_id in list(self._id_to_cb.keys()):
            user32.UnregisterHotKey(None, hk_id)
        self._id_to_cb.clear()
        log.info("HotkeyPump exited — all hotkeys released")
