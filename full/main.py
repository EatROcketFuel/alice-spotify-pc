#!/usr/bin/env python3
"""
Alice PC Controller
===================

Background Windows 11 tray application that bridges Yandex Alice (through an
MQTT broker) to the local PC:

* ``alice/pc/spotify/power``  -> ON / OFF        : launch (minimized) / close Spotify
* ``alice/pc/media/control``  -> NEXT / PREV / PLAY_PAUSE (+ STOP, VOLUME_UP,
                                 VOLUME_DOWN, MUTE): global hardware media keys
* ``alice/pc/audio/switch``   -> SPEAKERS / HEADPHONES : default playback device
* ``alice/pc/spotify/search`` -> "Song - Artist" : search via Spotify Web API and
                                 play on the local desktop client

Run modes (see ``python main.py --help``):

* ``pythonw main.py``               normal background mode with tray icon
* ``python main.py --no-tray``      console mode (debugging), Ctrl+C to exit
* ``python main.py --auth``         one-time Spotify login (creates token cache)
* ``python main.py --list-devices`` print Windows playback device names
* ``python main.py --settings``     settings window (also: tray menu -> Настройки)
* ``python main.py --test media_control NEXT``  run one command locally
* ``python main.py --send alice/pc/media/control NEXT``  publish via the broker
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import ctypes
import json
import logging
import logging.handlers
import os
import queue
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

APP_NAME = "Alice PC Controller"
APP_VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
IS_WINDOWS = sys.platform == "win32"

log = logging.getLogger("alice_pc")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Every key has a default here, so config.json only needs the values you change.
DEFAULT_CONFIG: dict[str, Any] = {
    "mqtt": {
        "host": "",
        "port": 1883,
        "username": "",
        "password": "",
        "client_id": "",              # empty -> "alice-pc-<hostname>"
        "keepalive": 60,
        "qos": 1,
        "tls": False,
        "tls_insecure": False,
        "ca_certs": "",
        "reconnect_min_delay": 1,
        "reconnect_max_delay": 60,
        "ignore_retained": True,      # never replay old retained commands on connect
        "status_topic": "alice/pc/status",
        "state_prefix": "alice/pc/state",
    },
    "topics": {
        "spotify_power": "alice/pc/spotify/power",
        "media_control": "alice/pc/media/control",
        "audio_switch": "alice/pc/audio/switch",
        "spotify_search": "alice/pc/spotify/search",
    },
    # Extra "button" topics: a message on the topic runs <command> with a fixed
    # <payload>. Handy for Smart Home on/off devices (see README).
    "button_topics": [],
    # Per-command payload translation, e.g. {"audio_switch": {"1": "SPEAKERS"}}.
    "payload_aliases": {},
    "spotify": {
        "exe_path": r"%APPDATA%\Spotify\Spotify.exe",
        "fallback_exe_paths": [
            r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe",
            r"%LOCALAPPDATA%\Spotify\Spotify.exe",
        ],
        "launch_args": ["--minimized"],
        "process_name": "Spotify.exe",
        "close_timeout_seconds": 6,
        "pause_before_close": False,
        "prev_presses": 2,            # Spotify restarts the track on the 1st "previous" press
        "search_mode": "auto",        # auto | keyless | web_api (auto: web_api once authorised)
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://127.0.0.1:8888/callback",
        "cache_path": ".spotify_token_cache",
        "device_name": "",            # empty -> this computer's name
        "market": "from_token",
        "auto_launch_on_search": True,
        "device_wait_seconds": 25,
        "tray_only": False,           # hide Spotify's window when this app launches it / opens a track
        "warmup_seconds": 4,          # hidden Spotify only plays after its window was shown once
        "play_on_launch": False,      # press Play after "turn Spotify on"
        "window_class": "Chrome_WidgetWin_0",
    },
    "audio": {
        "backend": "auto",            # auto | pycaw | powershell
        "mode": "app",                # app = only app_routing.processes | system = Windows default
        "devices": {
            "SPEAKERS": ["Speakers"],
            "HEADPHONES": ["Headphones"],
        },
        "roles": ["console", "multimedia", "communications"],
        "app_routing": {
            "enabled": False,
            "soundvolumeview_path": "tools/SoundVolumeView/SoundVolumeView.exe",
            "processes": ["Spotify.exe"],
        },
    },
    "logging": {
        "level": "INFO",
        "file": "logs/alice_pc.log",
        "max_bytes": 1_048_576,
        "backup_count": 5,
    },
    "tray": {
        "notifications": True,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):       # "_comment" keys are documentation only
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict:
    """Load config.json (UTF-8, BOM tolerated - Notepad adds one) over the defaults."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8-sig") as fh:
        try:
            user_cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"config.json is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}. "
                "Tip: backslashes in Windows paths must be doubled, e.g. C:\\\\Users\\\\..."
            ) from exc
    return deep_merge(DEFAULT_CONFIG, user_cfg)


def resolve_path(value: str) -> Path:
    """Expand %ENV% variables and make relative paths relative to the app folder."""
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else BASE_DIR / path


def update_config_file(path: Path, updates: dict) -> None:
    """Write ``updates`` into config.json, keeping every other key and the _comment notes."""
    data = json.loads(path.read_text(encoding="utf-8-sig") or "{}") if path.exists() else {}

    def merge(target: dict, source: dict) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = value

    merge(data, updates)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Autostart (shortcut in the user's Startup folder, same as add_to_startup.bat)
# --------------------------------------------------------------------------- #

CREATE_NO_WINDOW = 0x08000000


def startup_shortcut() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / "Alice PC Controller.lnk")


def autostart_enabled() -> bool:
    return startup_shortcut().exists()


def set_autostart(enabled: bool) -> None:
    link = startup_shortcut()
    if not enabled:
        link.unlink(missing_ok=True)
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pythonw = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    link.parent.mkdir(parents=True, exist_ok=True)
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:ALICE_LNK);"
              "$s.TargetPath=$env:ALICE_TARGET;$s.Arguments=$env:ALICE_ARGS;"
              "$s.WorkingDirectory=$env:ALICE_DIR;$s.WindowStyle=7;"
              "$s.Description='Alice PC Controller (MQTT -> Spotify / audio)';$s.Save()")
    env = {**os.environ, "ALICE_LNK": str(link), "ALICE_TARGET": str(pythonw),
           "ALICE_ARGS": f'"{BASE_DIR / "main.py"}"', "ALICE_DIR": str(BASE_DIR)}
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", script],
        env=env, capture_output=True, timeout=60, check=False,
        creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
    )
    if proc.returncode != 0 or not link.exists():
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Could not create the Startup shortcut: {err}")
    log.info("Autostart shortcut created: %s", link)


# --------------------------------------------------------------------------- #
# Logging / error helpers
# --------------------------------------------------------------------------- #

def setup_logging(cfg: dict, console: bool) -> Path:
    log_cfg = cfg["logging"]
    log_file = resolve_path(log_cfg["file"])
    log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(threadName)-12s %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, str(log_cfg["level"]).upper(), logging.INFO))

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=int(log_cfg["max_bytes"]),
        backupCount=int(log_cfg["backup_count"]), encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # pythonw.exe has no console (sys.stderr is None) - log to file only then.
    if console and sys.stderr is not None:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(errors="replace")   # Cyrillic on cp866/cp1251 consoles
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    for noisy in ("urllib3", "comtypes", "PIL", "spotipy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def _excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args: threading.ExceptHookArgs):
        log.critical("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    return log_file


def show_error_box(message: str) -> None:
    """Visible error even when started via pythonw.exe (no console)."""
    if sys.stderr is not None:
        print(message, file=sys.stderr)
        return
    if IS_WINDOWS:
        with contextlib.suppress(Exception):  # last-resort reporting
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)  # MB_ICONERROR


class SingleInstance:
    """Named mutex so the app cannot run twice (e.g. Startup + manual start)."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = "Local\\AlicePCController") -> None:
        self._handle = None
        self.already_running = False
        if not IS_WINDOWS:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._handle = kernel32.CreateMutexW(None, False, name)
        self.already_running = ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #

ON_WORDS = {"on", "1", "true", "yes", "start", "вкл", "включить", "включи"}
OFF_WORDS = {"off", "0", "false", "no", "stop", "выкл", "выключить", "выключи"}


def normalize_token(payload: str) -> str:
    """'play pause' / 'play-pause' / 'Play_Pause' -> 'PLAY_PAUSE'."""
    return re.sub(r"[\s\-]+", "_", payload.strip()).upper()


def apply_alias(aliases: dict, command: str, payload: str) -> str:
    """Translate a raw payload via config "payload_aliases" (case-insensitive)."""
    table = aliases.get(command) or {}
    key = payload.strip().casefold()
    for raw, mapped in table.items():
        if str(raw).strip().casefold() == key:
            return str(mapped)
    return payload


# --------------------------------------------------------------------------- #
# Media keys (global: work with Spotify in the tray and in fullscreen games)
# --------------------------------------------------------------------------- #

class MediaKeys:
    """
    Injects real media-key presses with the Win32 ``SendInput`` API - the same
    mechanism ``keyboard`` / ``pyautogui`` use internally, without the extra
    dependency. Windows routes media keys to the session's media controller, so
    Spotify reacts even when it is hidden in the tray or a game has focus.
    """

    VK: ClassVar[dict[str, int]] = {
        "NEXT": 0xB0,         # VK_MEDIA_NEXT_TRACK
        "PREV": 0xB1,         # VK_MEDIA_PREV_TRACK
        "STOP": 0xB2,         # VK_MEDIA_STOP
        "PLAY_PAUSE": 0xB3,   # VK_MEDIA_PLAY_PAUSE
        "MUTE": 0xAD,         # VK_VOLUME_MUTE
        "VOLUME_DOWN": 0xAE,  # VK_VOLUME_DOWN
        "VOLUME_UP": 0xAF,    # VK_VOLUME_UP
    }
    ALIASES: ClassVar[dict[str, str]] = {
        "NEXT_TRACK": "NEXT", "FORWARD": "NEXT", "СЛЕДУЮЩИЙ": "NEXT",
        "PREVIOUS": "PREV", "PREV_TRACK": "PREV", "PREVIOUS_TRACK": "PREV",
        "BACK": "PREV", "ПРЕДЫДУЩИЙ": "PREV",
        "PLAYPAUSE": "PLAY_PAUSE", "PLAY": "PLAY_PAUSE", "PAUSE": "PLAY_PAUSE",
        "TOGGLE": "PLAY_PAUSE", "ПАУЗА": "PLAY_PAUSE",
        "VOL_UP": "VOLUME_UP", "VOL_DOWN": "VOLUME_DOWN",
    }

    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002

    def __init__(self) -> None:
        self._send_input = None
        if IS_WINDOWS:
            self._init_win32()

    def _init_win32(self) -> None:
        from ctypes import wintypes

        ulong_ptr = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        self._INPUT = INPUT
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        user32.MapVirtualKeyW.restype = wintypes.UINT
        self._user32 = user32
        self._send_input = user32.SendInput

    @classmethod
    def resolve(cls, payload: str) -> str | None:
        token = normalize_token(payload)
        token = cls.ALIASES.get(token, token)
        return token if token in cls.VK else None

    def press(self, action: str) -> None:
        if self._send_input is None:
            raise RuntimeError("Media keys are only supported on Windows")
        vk = self.VK[action]
        scan = self._user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        inputs = (self._INPUT * 2)()
        for i, flags in enumerate((self.KEYEVENTF_EXTENDEDKEY,
                                   self.KEYEVENTF_EXTENDEDKEY | self.KEYEVENTF_KEYUP)):
            inputs[i].type = self.INPUT_KEYBOARD
            inputs[i].ki.wVk = vk
            inputs[i].ki.wScan = scan
            inputs[i].ki.dwFlags = flags
        sent = self._send_input(2, inputs, ctypes.sizeof(self._INPUT))
        if sent != 2:
            raise OSError(ctypes.get_last_error(),
                          "SendInput was blocked (an elevated/admin window may be in the foreground)")


# --------------------------------------------------------------------------- #
# Spotify desktop process control
# --------------------------------------------------------------------------- #

class SpotifyProcess:
    """Starts Spotify minimized to the tray and closes it gracefully."""

    WM_CLOSE = 0x0010
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    SW_HIDE = 0
    SW_SHOWNA = 8
    SW_RESTORE = 9
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_LAYERED = 0x00080000
    LWA_ALPHA = 0x2

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg["spotify"]
        self._hidden: set[int] = set()
        self._warmed: set[int] = set()

    @property
    def process_name(self) -> str:
        return self.cfg["process_name"]

    def find_exe(self) -> Path | None:
        for candidate in [self.cfg["exe_path"], *self.cfg.get("fallback_exe_paths", [])]:
            if not candidate:
                continue
            path = resolve_path(candidate)
            if path.exists():
                return path
        return None

    def processes(self) -> list:
        import psutil
        name = self.process_name.casefold()
        found = []
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info["name"] or "").casefold() == name:
                    found.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found

    def is_running(self) -> bool:
        return bool(self.processes())

    def launch(self) -> bool:
        """Start Spotify. Returns False if it was already running."""
        if self.is_running():
            log.info("Spotify is already running")
            return False
        exe = self.find_exe()
        if exe is None:
            raise FileNotFoundError(
                f"Spotify.exe not found (checked {self.cfg['exe_path']} and fallbacks). "
                "Set spotify.exe_path in config.json")
        args = [str(exe), *[str(a) for a in self.cfg.get("launch_args", [])]]
        log.info("Launching Spotify: %s", " ".join(args))
        flags = (self.DETACHED_PROCESS | self.CREATE_NEW_PROCESS_GROUP) if IS_WINDOWS else 0
        subprocess.Popen(
            args, cwd=str(exe.parent), close_fds=True, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True

    def _windows_of(self, pids: set[int]) -> list[int]:
        """Top-level windows with a title that belong to the given processes."""
        if not IS_WINDOWS:
            return []
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        handles: list[int] = []
        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.GetWindowTextLengthW(hwnd) > 0:
                handles.append(hwnd)
            return True

        user32.EnumWindows(enum_proc_type(_callback), 0)
        return handles

    def visible_windows(self) -> list[int]:
        procs = self.processes() if IS_WINDOWS else []
        if not procs:
            return []
        user32 = ctypes.windll.user32
        return [h for h in self._windows_of({p.pid for p in procs}) if user32.IsWindowVisible(h)]

    def hide_window(self, wait: float = 0.0, settle: float = 2.0) -> int:
        """Tray-only mode: hide Spotify's window (its tray icon or our menu brings it back).
        Waits up to ``wait`` s for the window, then keeps it hidden ``settle`` s more because
        Spotify re-shows it while starting or opening a link."""
        if not IS_WINDOWS:
            return 0
        user32 = ctypes.windll.user32
        hidden = 0
        deadline = time.monotonic() + wait
        while True:
            for hwnd in self.visible_windows():
                user32.ShowWindow(hwnd, self.SW_HIDE)
                self._hidden.add(hwnd)
                if not hidden:
                    deadline = time.monotonic() + settle
                hidden += 1
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if hidden:
            log.info("Spotify window hidden (tray only)")
        return hidden

    def show_window(self) -> bool:
        """Bring back a window hidden by hide_window (or by a previous run of this app)."""
        if not IS_WINDOWS:
            return False
        user32 = ctypes.windll.user32
        handles = [h for h in self._hidden if user32.IsWindow(h)] or self.visible_windows()
        if not handles and (main := self.main_window()):
            handles.append(main)
        for hwnd in handles:
            user32.ShowWindow(hwnd, self.SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        self._hidden.clear()
        if not handles:
            log.info("No Spotify window to show (is Spotify running?)")
        return bool(handles)

    def main_window(self, wait: float = 0.0) -> int | None:
        """Spotify's main window, visible or hidden (waits up to ``wait`` s for it to appear)."""
        if not IS_WINDOWS:
            return None
        user32 = ctypes.windll.user32
        wanted = self.cfg.get("window_class") or "Chrome_WidgetWin_0"
        buf = ctypes.create_unicode_buffer(256)
        deadline = time.monotonic() + wait
        while True:
            pids = {p.pid for p in self.processes()}
            fallback = None
            for hwnd in self._windows_of(pids) if pids else []:
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value == wanted:
                    return hwnd
                if fallback is None and buf.value.startswith("Chrome_WidgetWin"):
                    fallback = hwnd
            if fallback or time.monotonic() >= deadline:
                return fallback
            time.sleep(0.1)

    def warm_up(self, hwnd: int | None = None, wait: float = 25.0) -> bool:
        """Spotify started hidden ignores Play until its window has been on screen once.
        Show the window fully transparent, without focus or a taskbar button, let it render
        for ``warmup_seconds`` and hide it again."""
        if not IS_WINDOWS:
            return False
        hwnd = hwnd or self.main_window(wait)
        if not hwnd:
            log.warning("Spotify window not found - background warm-up skipped")
            return False
        user32 = ctypes.windll.user32
        ex = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
        temp = (ex | self.WS_EX_LAYERED | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
        seconds = float(self.cfg.get("warmup_seconds", 4))
        try:
            user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, temp)
            user32.SetLayeredWindowAttributes(hwnd, 0, 0, self.LWA_ALPHA)
            user32.ShowWindow(hwnd, self.SW_SHOWNA)
            time.sleep(seconds)
        finally:
            user32.ShowWindow(hwnd, self.SW_HIDE)
            if ex & self.WS_EX_LAYERED:
                user32.SetLayeredWindowAttributes(hwnd, 0, 255, self.LWA_ALPHA)
            user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, ex)
        self._warmed.add(hwnd)
        self._hidden.add(hwnd)
        log.info("Spotify warmed up in the background (invisible window for %.0f s)", seconds)
        return True

    def ensure_ready(self, wait: float = 0.0) -> None:
        """Warm Spotify up if its window has not been on screen since this app saw it."""
        if not IS_WINDOWS or not self.is_running():
            return
        hwnd = self.main_window(wait)
        if not hwnd or hwnd in self._warmed:
            return
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            self._warmed.add(hwnd)      # open (or minimised) - it already works
            return
        self.warm_up(hwnd)

    def start_hidden(self, wait: float = 25.0) -> None:
        """Tray-only launch: warm the new window up invisibly and keep it hidden."""
        hwnd = self.main_window(wait)
        if hwnd:
            self.warm_up(hwnd)
        self.hide_window(wait=0, settle=2)

    def close(self) -> bool:
        """Ask Spotify to quit (WM_CLOSE); force-terminate after a timeout."""
        import psutil
        procs = self.processes()
        if not procs:
            log.info("Spotify is not running")
            return False

        windows = self._windows_of({p.pid for p in procs})
        log.info("Closing Spotify gracefully (%d processes, %d windows)", len(procs), len(windows))
        for hwnd in windows:
            ctypes.windll.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)

        timeout = float(self.cfg.get("close_timeout_seconds", 6))
        _, alive = psutil.wait_procs(procs, timeout=timeout if windows else 0.5)
        if alive:
            # The window only hid into the tray ("close button minimizes" option)
            # or the app hangs -> terminate the remaining processes.
            log.info("Spotify still running, terminating %d processes", len(alive))
            for proc in alive:
                try:
                    proc.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(alive, timeout=5)
            for proc in alive:
                try:
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass
        log.info("Spotify closed")
        return True


# --------------------------------------------------------------------------- #
# Windows audio output switching
# --------------------------------------------------------------------------- #

class AudioDevice:
    def __init__(self, device_id: str, name: str, is_default: bool = False) -> None:
        self.id = device_id
        self.name = name
        self.is_default = is_default

    def __repr__(self) -> str:
        return f"AudioDevice({self.name!r}{', default' if self.is_default else ''})"


class PycawBackend:
    """Native Core Audio API through pycaw (IMMDeviceEnumerator + IPolicyConfig)."""

    name = "pycaw"
    ROLE_NAMES: ClassVar[dict[str, str]] = {"console": "eConsole", "multimedia": "eMultimedia",
                  "communications": "eCommunications"}

    def __init__(self) -> None:
        from pycaw.pycaw import AudioUtilities
        if not hasattr(AudioUtilities, "SetDefaultDevice"):
            raise ImportError("pycaw is too old (AudioUtilities.SetDefaultDevice missing)")

    def list_devices(self) -> list[AudioDevice]:
        from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
        from pycaw.pycaw import AudioUtilities

        default_id = None
        try:
            enumerator = AudioUtilities.GetDeviceEnumerator()
            default_id = enumerator.GetDefaultAudioEndpoint(
                EDataFlow.eRender.value, ERole.eMultimedia.value).GetId()
        except Exception:  # noqa: BLE001 - "no default device" is a valid state
            log.debug("No default playback device")
        result = []
        for dev in AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value):
            result.append(AudioDevice(dev.id, dev.FriendlyName or "", dev.id == default_id))
        return result

    def set_default(self, device: AudioDevice, roles: list[str]) -> None:
        from pycaw.constants import ERole
        from pycaw.pycaw import AudioUtilities
        role_enums = [getattr(ERole, self.ROLE_NAMES[r]) for r in roles if r in self.ROLE_NAMES]
        AudioUtilities.SetDefaultDevice(device.id, roles=role_enums or [ERole.eConsole])


class PowerShellBackend:
    """Wrapper around the AudioDeviceCmdlets PowerShell module."""

    name = "powershell"
    CREATE_NO_WINDOW = 0x08000000

    def _run(self, script: str) -> str:
        full = ("$ErrorActionPreference='Stop';"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "Import-Module AudioDeviceCmdlets;" + script)
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", full],
            check=False, capture_output=True, timeout=30,
            creationflags=self.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"PowerShell/AudioDeviceCmdlets failed: {err or out}")
        return out

    def list_devices(self) -> list[AudioDevice]:
        out = self._run("@(Get-AudioDevice -List | Where-Object { $_.Type -eq 'Playback' } | "
                        "Select-Object ID,Name,Default) | ConvertTo-Json -Compress")
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return [AudioDevice(d["ID"], d["Name"], bool(d.get("Default"))) for d in data]

    def set_default(self, device: AudioDevice, roles: list[str]) -> None:
        dev_id = device.id.replace("'", "''")
        self._run(f"Set-AudioDevice -ID '{dev_id}' | Out-Null")


SPEAKER_HINTS = ("станци", "station", "яндекс", "yandex", "алиса", "alice", "колонк", "jbl",
                 "marshall", "sonos", "soundbar", "саундбар", "динамик", "speaker")
HEADPHONE_HINTS = ("наушник", "headphone", "headset", "гарнитур", "airpods", "buds", "earphone")


def guess_device(names: list[str], hints: tuple[str, ...], exclude: str = "") -> str | None:
    """Best device for a target: an earlier hint is stronger, Bluetooth Hands-Free loses."""
    best = None
    for name in names:
        low = name.casefold()
        ranks = [i for i, hint in enumerate(hints) if hint in low]
        if not ranks or name == exclude:
            continue
        key = ("hands-free" in low or "hands free" in low, min(ranks))
        if best is None or key < best[0]:
            best = (key, name)
    return best[1] if best else None


class AudioSwitcher:
    """Maps logical targets (SPEAKERS / HEADPHONES / ...) to real playback devices."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg["audio"]
        self._backend = None
        self.pending_target: str | None = None  # app mode: target to apply once Spotify starts

    @property
    def app_mode(self) -> bool:
        """mode=app: route only app_routing.processes, keep the Windows default device."""
        return str(self.cfg.get("mode", "app")).strip().lower() == "app"

    @property
    def backend(self):
        if self._backend is None:
            choice = str(self.cfg.get("backend", "auto")).lower()
            if choice in ("auto", "pycaw"):
                try:
                    self._backend = PycawBackend()
                except Exception as exc:
                    if choice == "pycaw":
                        raise
                    log.warning("pycaw unavailable (%s), using the PowerShell backend", exc)
            if self._backend is None:
                self._backend = PowerShellBackend()
            log.info("Audio backend: %s", self._backend.name)
        return self._backend

    def mapping(self) -> dict[str, list[str]]:
        result = {}
        for key, value in self.cfg["devices"].items():
            result[key.upper()] = [value] if isinstance(value, str) else list(value)
        return result

    def list_devices(self) -> list[AudioDevice]:
        return self.backend.list_devices()

    @staticmethod
    def match(devices: list[AudioDevice], wanted: list[str]) -> AudioDevice | None:
        """Exact (case-insensitive) name first, then substring. Bluetooth "Hands-Free"
        endpoints (mono, low quality) lose against the stereo endpoint."""
        for name in wanted:
            key = name.strip().casefold()
            if not key:
                continue
            for dev in devices:
                if dev.name.casefold() == key:
                    return dev
            partial = [d for d in devices if key in d.name.casefold()]
            if partial:
                partial.sort(key=lambda d: "hands-free" in d.name.casefold()
                             or "hands free" in d.name.casefold())
                return partial[0]
        return None

    def switch(self, target: str) -> AudioDevice:
        target = target.upper()
        mapping = self.mapping()
        if target not in mapping:
            raise ValueError(f"Unknown audio target {target!r}. Configured: {', '.join(mapping)}")

        devices = self.list_devices()
        device = self.match(devices, mapping[target])
        if device is None:
            available = "; ".join(d.name for d in devices) or "no active playback devices"
            raise LookupError(f"No playback device matches {mapping[target]} for {target}. "
                              f"Available: {available}")

        if self.app_mode:
            if self._route_apps(device, required=True):
                self.pending_target = None
            else:
                self.pending_target = target
                log.info("No running %s - %s will be applied when it starts",
                         ", ".join(self.app_processes()), device.name)
            return device

        if device.is_default:
            log.info("%s is already the default output: %s", target, device.name)
        else:
            self.backend.set_default(device, list(self.cfg.get("roles", ["console", "multimedia"])))
            log.info("Default output switched to %s: %s", target, device.name)
        self._route_apps(device)
        return device

    def apply_pending(self) -> None:
        """App mode: route Spotify to the target requested while it was not running."""
        if self.app_mode and self.pending_target:
            self.switch(self.pending_target)

    def app_processes(self) -> list[str]:
        return list((self.cfg.get("app_routing") or {}).get("processes") or ["Spotify.exe"])

    def _route_apps(self, device: AudioDevice, required: bool = False) -> int:
        """Per-app output (Settings > Volume mixer > App > Output device) via NirSoft
        SoundVolumeView /SetAppDefault. Windows remembers it per app. Returns the number
        of routed processes."""
        routing = self.cfg.get("app_routing") or {}
        if not (required or routing.get("enabled")):
            return 0
        bundled = DEFAULT_CONFIG["audio"]["app_routing"]["soundvolumeview_path"]
        candidates = [resolve_path(p) for p in (routing.get("soundvolumeview_path"), bundled) if p]
        exe = next((p for p in candidates if p.exists()), candidates[0])
        if not exe.exists():
            message = f"SoundVolumeView not found at {exe} (needed for per-app audio, see README)"
            if required:
                raise FileNotFoundError(message)
            log.warning(message)
            return 0
        import psutil

        names = {n.casefold() for n in self.app_processes()}
        pids = []
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").casefold() in names:
                pids.append(proc.pid)
        for pid in pids:  # every process: Spotify plays audio from one of its child processes
            try:
                result = subprocess.run(
                    [str(exe), "/SetAppDefault", device.id, "all", str(pid)],
                    check=False, capture_output=True, timeout=15,
                    creationflags=PowerShellBackend.CREATE_NO_WINDOW if IS_WINDOWS else 0)
            except subprocess.TimeoutExpired:
                log.warning("SoundVolumeView /SetAppDefault for PID %s timed out", pid)
                continue
            if result.returncode != 0:
                log.warning("SoundVolumeView /SetAppDefault for PID %s exited with %s",
                            pid, result.returncode)
        if pids:
            log.info("Routed %s (%d processes) to %s", ", ".join(self.app_processes()),
                     len(pids), device.name)
        return len(pids)


# --------------------------------------------------------------------------- #
# Spotify Web API (search + play on the local desktop client)
# --------------------------------------------------------------------------- #

class SpotifyAuthRequired(RuntimeError):
    """Raised in background mode instead of opening a browser / waiting for input."""


SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"


def build_spotify_oauth(cfg: dict, interactive: bool, open_browser: bool = True):
    """
    Spotipy OAuth manager backed by a token cache file.

    In background mode (``interactive=False``) it only uses / refreshes the cached
    token and NEVER opens a browser or waits for console input: a missing or
    revoked token raises :class:`SpotifyAuthRequired` instead of freezing the app.
    ``python main.py --auth`` creates the cache once; spotipy refreshes it after.
    """
    from spotipy.cache_handler import CacheFileHandler
    from spotipy.oauth2 import SpotifyOAuth

    scfg = cfg["spotify"]
    if not scfg.get("client_id") or not scfg.get("client_secret"):
        raise SpotifyAuthRequired("spotify.client_id / client_secret are empty in config.json")

    class _OAuth(SpotifyOAuth):
        def get_auth_response(self, open_browser=None):
            if not interactive:
                raise SpotifyAuthRequired(
                    "Spotify token is missing or revoked. Run 'python main.py --auth' once.")
            return super().get_auth_response(open_browser)

    cache_path = resolve_path(scfg["cache_path"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    return _OAuth(
        client_id=scfg["client_id"],
        client_secret=scfg["client_secret"],
        redirect_uri=scfg["redirect_uri"],
        scope=SPOTIFY_SCOPES,
        cache_handler=CacheFileHandler(cache_path=str(cache_path)),
        open_browser=open_browser,
        requests_timeout=15,
    )


_TRACK_URL_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?track/([A-Za-z0-9]{22})")
_SEPARATOR_RE = re.compile(r"\s+[-\u2013\u2014]\s+")  # " - ", " – ", " — "


def parse_search_payload(payload: str) -> tuple[str | None, str | None, str]:
    """'Song Name - Artist' -> (song, artist, raw). No separator -> (None, None, raw)."""
    raw = payload.strip().strip('"').strip()
    parts = _SEPARATOR_RE.split(raw, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip(), raw
    return None, None, raw


_LINK_ID_RE = re.compile(r"open\.spotify\.com(?:/|%2F)(?:intl-[a-z]+(?:/|%2F))?track(?:/|%2F)"
                         r"([A-Za-z0-9]{22})", re.IGNORECASE)
_URI_ID_RE = re.compile(r"spotify:track:([A-Za-z0-9]{22})")
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept-Language": "ru,en;q=0.8"}


def http_get(url: str, timeout: float = 10) -> str:
    import urllib.request
    request = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def open_uri(uri: str) -> None:
    if not IS_WINDOWS:
        raise RuntimeError("Opening spotify: links is only supported on Windows")
    os.startfile(uri)  # handled by the Spotify desktop client (spotify: protocol)


_TRANSLIT = dict(zip("абвгдезийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                      "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu",
                      "ya"]))
_TRANSLIT["ж"] = "zh"
# Alternative recordings (word prefixes): penalised unless the request mentions the same kind.
_VARIANT_KINDS = (
    ("remix", "rmx", "ремикс", "микс"),
    ("acoustic", "unplugged", "акуст"),
    ("live", "концерт", "вживую", "лайв"),
    ("cover", "кавер"),
    ("karaoke", "караоке", "минус", "instrumental", "инструментал"),
    ("sped", "slowed", "nightcore", "reverb", "speed", "замедл", "ускор"),
    ("bass", "8d"),
    ("demo", "демо"),
)
KEYLESS_CANDIDATES = 8


def _words(text: str) -> set[str]:
    """Lower-case words plus their Latin transliteration ("Кино" also matches "Kino")."""
    words = {w for w in re.findall(r"\w+", text.casefold().replace("ё", "е")) if len(w) > 1}
    return words | {"".join(_TRANSLIT.get(ch, ch) for ch in w) for w in words}


def track_meta(track_id: str) -> dict | None:
    """Title, artists, release date and playability (for this PC's region) from the public
    embed page; falls back to oEmbed (title only)."""
    try:
        page = http_get(f"https://open.spotify.com/embed/track/{track_id}")
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.DOTALL)
        stack = [json.loads(match.group(1))] if match else []
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if isinstance(node.get("artists"), list) and (node.get("name") or node.get("title")):
                    release = node.get("releaseDate")
                    return {
                        "title": str(node.get("name") or node.get("title")),
                        "artists": [x.get("name", "") for x in node["artists"] if isinstance(x, dict)],
                        "release": str((release or {}).get("isoString") or "")[:10],
                        "playable": node.get("isPlayable") is not False,
                    }
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    except Exception as exc:  # noqa: BLE001
        log.debug("Embed page failed for %s: %s", track_id, exc)
    try:
        data = json.loads(http_get("https://open.spotify.com/oembed?url="
                                   f"https://open.spotify.com/track/{track_id}"))
        return {"title": str(data.get("title") or track_id), "artists": [], "release": "",
                "playable": True}
    except Exception as exc:  # noqa: BLE001
        log.debug("oEmbed failed for %s: %s", track_id, exc)
        return None


def score_candidate(title: str, artists: list[str], song: str | None, artist: str | None,
                    raw: str) -> tuple[float, bool]:
    """(score, artist_matched). Higher score = the original recording by the requested artist."""
    query = _words(raw)
    title_words = _words(title)
    score = 0.0
    raw_title = re.findall(r"\w+", title.casefold())
    raw_query = re.findall(r"\w+", raw.casefold())
    for prefixes in _VARIANT_KINDS:
        in_title = any(w.startswith(prefixes) for w in raw_title)
        if in_title and not any(w.startswith(prefixes) for w in raw_query):
            score -= 10
    core = _words(re.sub(r"[(\[].*?[)\]]|\s[-\u2013\u2014]\s.*$", " ", title))
    if core and core <= query:
        score += 5
    halves = [_words(h) for h in (song, artist) if h]
    if any(h and h <= title_words for h in halves):
        score += 3
    artist_words = set().union(*(_words(x) for x in artists)) if artists else set()
    matched = False
    if artist:
        matched = bool(artist_words & (halves[0] | halves[1]) - title_words)
        if matched:
            score += 6
        elif artist_words:
            score -= 4
    elif artist_words & (query - core):
        matched = True
        score += 4
    return score, matched


KEYLESS_SEARCH_URLS = ("https://html.duckduckgo.com/html/?q=", "https://lite.duckduckgo.com/lite/?q=")


def keyless_find_track(payload: str) -> tuple[str, str]:
    """Find a track without Spotify keys: DuckDuckGo HTML results for open.spotify.com/track
    links, titles checked via the public Spotify oEmbed endpoint. Returns (track_id, title)."""
    import html
    import urllib.parse

    direct = _LINK_ID_RE.search(payload) or _URI_ID_RE.search(payload)
    if direct:
        return direct.group(1), direct.group(1)

    song, artist, raw = parse_search_payload(payload)
    text = f"{song} {artist}" if song else raw
    ids: list[str] = []
    for engine in KEYLESS_SEARCH_URLS:
        for query in (f"spotify track {text}", f"site:open.spotify.com/track {text}"):
            try:
                page = html.unescape(http_get(engine + urllib.parse.quote_plus(query)))
            except Exception as exc:  # noqa: BLE001 - try the next engine / query
                log.warning("Keyless search failed (%s): %s", engine.split("/")[2], exc)
                break
            if "anomaly" in page and "open.spotify.com" not in page:
                log.warning("Keyless search: %s asked for a captcha", engine.split("/")[2])
                break
            for found in _LINK_ID_RE.findall(page):
                if found not in ids:
                    ids.append(found)
            if ids:
                break
        if ids:
            break
    if not ids:
        raise LookupError(f"Nothing found on Spotify for {payload!r} (keyless search)")

    from concurrent.futures import ThreadPoolExecutor

    top = ids[:KEYLESS_CANDIDATES]
    with ThreadPoolExecutor(max_workers=len(top)) as pool:
        metas = list(pool.map(track_meta, top))
    rows = []
    for rank, (track_id, meta) in enumerate(zip(top, metas, strict=True)):
        if meta is None:
            continue
        score, matched = score_candidate(meta["title"], meta["artists"], song, artist, raw)
        score -= rank * 0.3
        if not meta["playable"]:
            score -= 20                    # greyed out in this region
        rows.append([score, matched, track_id, meta])
    # Same artist, several releases of the song: the earliest one is usually the original
    # (later ones are acoustic albums, compilations, re-recordings).
    dates = sorted(r[3]["release"] for r in rows if r[1] and r[3]["release"])
    if len(dates) > 1 and dates[0] != dates[-1]:
        first, last = _days(dates[0]), _days(dates[-1])
        for row in rows:
            if row[1] and row[3]["release"]:
                row[0] += 3 * (last - _days(row[3]["release"])) / (last - first)
    best = None
    for score, _matched, track_id, meta in rows:
        label = f"{', '.join(meta['artists'])} — {meta['title']}" if meta["artists"] else meta["title"]
        log.info("  candidate %5.1f  %s [%s]%s", score, label, meta["release"] or "?",
                 "" if meta["playable"] else " unavailable")
        if best is None or score > best[0]:
            best = (score, track_id, label)
    return (best[1], best[2]) if best else (ids[0], text)


def _days(iso_date: str) -> int:
    import datetime
    try:
        return datetime.date.fromisoformat(iso_date[:10]).toordinal()
    except ValueError:
        return 0


def describe_track(track: dict) -> str:
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    return f"{artists} — {track.get('name', '?')}"


class SpotifyController:
    def __init__(self, cfg: dict, process: SpotifyProcess) -> None:
        self.cfg = cfg
        self.scfg = cfg["spotify"]
        self.process = process
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import spotipy
            self._client = spotipy.Spotify(
                auth_manager=build_spotify_oauth(self.cfg, interactive=False),
                requests_timeout=15, retries=3)
        return self._client

    def is_authorized(self) -> bool:
        """True if a token cache exists (refresh happens lazily on first API call)."""
        try:
            oauth = build_spotify_oauth(self.cfg, interactive=False)
            token = oauth.cache_handler.get_cached_token()
            return bool(token and token.get("refresh_token"))
        except Exception:  # noqa: BLE001
            return False

    # -- search ------------------------------------------------------------ #
    def find_track(self, payload: str) -> dict | None:
        match = _TRACK_URL_RE.search(payload)
        if match:
            return self.client.track(match.group(1))
        if payload.strip().startswith("spotify:track:"):
            return self.client.track(payload.strip())

        song, artist, raw = parse_search_payload(payload)
        market = self.scfg.get("market") or None
        if song and artist:
            queries = [f'track:"{song}" artist:"{artist}"',
                       f'track:"{artist}" artist:"{song}"',   # "Artist - Song" order
                       f"{song} {artist}"]
        else:
            queries = [raw]
        for query in queries:
            log.debug("Spotify search: %s", query)
            items = self.client.search(q=query, type="track", limit=5, market=market)["tracks"]["items"]
            if items:
                return items[0]
        return None

    # -- device selection -------------------------------------------------- #
    def pick_device(self, devices: list[dict]) -> dict | None:
        wanted = (self.scfg.get("device_name") or socket.gethostname()).casefold()
        for dev in devices:
            if dev.get("name", "").casefold() == wanted:
                return dev
        computers = [d for d in devices if d.get("type") == "Computer"]
        for dev in computers:
            if dev.get("is_active"):
                return dev
        return computers[0] if computers else None

    def wait_for_device(self) -> dict | None:
        device = self.pick_device(self.client.devices().get("devices", []))
        if device:
            return device
        if self.scfg.get("auto_launch_on_search", True) and not self.process.is_running():
            self.process.launch()
        deadline = time.monotonic() + float(self.scfg.get("device_wait_seconds", 25))
        while time.monotonic() < deadline:
            time.sleep(1.5)
            device = self.pick_device(self.client.devices().get("devices", []))
            if device:
                return device
        return None

    # -- play -------------------------------------------------------------- #
    def use_web_api(self) -> bool:
        mode = str(self.scfg.get("search_mode", "auto")).strip().lower()
        if mode == "web_api":
            return True
        return mode != "keyless" and self.is_authorized()

    def play(self, payload: str) -> str:
        return self.play_web_api(payload) if self.use_web_api() else self.play_keyless(payload)

    def play_keyless(self, payload: str) -> str:
        """No Premium / keys needed: open spotify:track:<id> in the desktop client."""
        try:
            track_id, title = keyless_find_track(payload)
            uri = f"spotify:track:{track_id}"
            log.info("Found (keyless): %s (%s)", title, uri)
        except LookupError as exc:   # no result / captcha: at least show Spotify's own search
            import urllib.parse
            song, artist, raw = parse_search_payload(payload)
            text = f"{song} {artist}" if song else raw
            uri, title = "spotify:search:" + urllib.parse.quote(text), f"поиск «{text}»"
            log.warning("%s - opening the search page in Spotify instead", exc)
        tray_only = bool(self.scfg.get("tray_only"))
        if not self.process.is_running() and self.scfg.get("auto_launch_on_search", True):
            self.process.launch()
            deadline = time.monotonic() + float(self.scfg.get("device_wait_seconds", 25))
            while not self.process.is_running() and time.monotonic() < deadline:
                time.sleep(0.5)
            if tray_only:
                self.process.start_hidden()
            time.sleep(3 if tray_only else 5)  # let the client log in before handing it the link
        self.process.ensure_ready()
        keep_hidden = tray_only and not self.process.visible_windows()
        open_uri(uri)
        if keep_hidden:
            self.process.hide_window(wait=3)
        return title

    def play_web_api(self, payload: str) -> str:
        from spotipy.exceptions import SpotifyException

        track = self.find_track(payload)
        if track is None:
            raise LookupError(f"Nothing found on Spotify for {payload!r}")
        title = describe_track(track)
        log.info("Found: %s (%s)", title, track["uri"])

        device = self.wait_for_device()
        if device is None:
            raise RuntimeError("Spotify desktop client is not visible to the Web API. "
                               "Make sure Spotify is running and logged in.")
        log.info("Playing on device %r (%s)", device.get("name"), device.get("type"))
        try:
            self.client.start_playback(device_id=device["id"], uris=[track["uri"]])
        except SpotifyException as exc:
            if exc.http_status != 404:
                raise
            # NO_ACTIVE_DEVICE: wake the device up first, then retry once.
            self.client.transfer_playback(device_id=device["id"], force_play=False)
            time.sleep(1.0)
            self.client.start_playback(device_id=device["id"], uris=[track["uri"]])
        return title


# --------------------------------------------------------------------------- #
# Command routing
# --------------------------------------------------------------------------- #

COMMANDS = ("spotify_power", "media_control", "audio_switch", "spotify_search")


class CommandRouter:
    """Turns (topic, payload) into actions. Runs on the worker thread."""

    def __init__(self, cfg: dict,
                 publish_state: Callable[[str, str], None] = lambda *_: None,
                 notify: Callable[[str], None] = lambda *_: None) -> None:
        self.cfg = cfg
        self.publish_state = publish_state
        self.notify = notify
        self.spotify_process = SpotifyProcess(cfg)
        self.media = MediaKeys()
        self.audio = AudioSwitcher(cfg)
        self.spotify = SpotifyController(cfg, self.spotify_process)
        self.handlers: dict[str, Callable[[str], None]] = {
            "spotify_power": self.spotify_power,
            "media_control": self.media_control,
            "audio_switch": self.audio_switch,
            "spotify_search": self.spotify_search,
        }
        self.routes = self._build_routes()

    def _build_routes(self) -> dict[str, tuple[str, str | None, str]]:
        """topic -> (command, fixed payload or None, trigger)."""
        routes: dict[str, tuple[str, str | None, str]] = {}
        for command, topic in self.cfg["topics"].items():
            if command not in self.handlers:
                log.warning("Unknown command %r in config 'topics' - ignored", command)
            elif topic:
                routes[topic] = (command, None, "any")
        for button in self.cfg.get("button_topics", []):
            command = button.get("command")
            if command not in self.handlers or not button.get("topic"):
                log.warning("Invalid button_topics entry %r - ignored", button)
                continue
            routes[button["topic"]] = (command, str(button.get("payload", "")),
                                       str(button.get("trigger", "any")).lower())
        return routes

    def handle_message(self, topic: str, payload: str) -> None:
        entry = self.routes.get(topic)
        if entry is None:
            log.warning("No handler for topic %s", topic)
            return
        command, fixed, trigger = entry
        if fixed is not None:
            if trigger == "on" and payload.strip().casefold() not in ON_WORDS:
                log.info("Button %s ignored payload %r (trigger=on)", topic, payload)
                return
            payload = fixed
        self.execute(command, payload)

    def execute(self, command: str, payload: str) -> bool:
        payload = apply_alias(self.cfg.get("payload_aliases", {}), command, payload)
        log.info("Command %s <- %r", command, payload)
        try:
            self.handlers[command](payload)
            return True
        except Exception as exc:
            log.error("Command %s(%r) failed: %s", command, payload, exc,
                      exc_info=log.isEnabledFor(logging.DEBUG))
            self.publish_state("error", f"{command}: {exc}")
            self.notify(f"Ошибка: {exc}")
            return False

    # -- handlers ---------------------------------------------------------- #
    def spotify_power(self, payload: str) -> None:
        word = payload.strip().casefold()
        if word in ON_WORDS:
            launched = self.spotify_process.launch()
            self.publish_state("spotify", "ON")
            if launched and self.cfg["spotify"].get("tray_only"):
                self.spotify_process.start_hidden()
            if launched and self.cfg["spotify"].get("play_on_launch"):
                self.spotify_process.ensure_ready(wait=25)
                time.sleep(1)
                self.media.press("PLAY_PAUSE")
            if self.audio.app_mode and self.audio.pending_target:
                time.sleep(4)  # let Spotify create its audio processes first
                self.audio.apply_pending()
        elif word in OFF_WORDS:
            if self.cfg["spotify"].get("pause_before_close") and self.spotify_process.is_running():
                self.media.press("PLAY_PAUSE")
                time.sleep(0.3)
            self.spotify_process.close()
            self.publish_state("spotify", "OFF")
        else:
            raise ValueError(f"spotify_power expects ON or OFF, got {payload!r}")

    def media_control(self, payload: str) -> None:
        action = MediaKeys.resolve(payload)
        if action is None:
            raise ValueError(f"Unknown media action {payload!r}. Use one of: {', '.join(MediaKeys.VK)}")
        self.spotify_process.ensure_ready()
        presses = max(1, int(self.cfg["spotify"].get("prev_presses", 2))) if action == "PREV" else 1
        for i in range(presses):
            if i:
                time.sleep(0.15)
            self.media.press(action)
        log.info("Media key sent: %s%s", action, f" x{presses}" if presses > 1 else "")

    def audio_switch(self, payload: str) -> None:
        target = normalize_token(payload)
        device = self.audio.switch(target)
        self.publish_state("audio", target)
        self.notify(f"Звук: {device.name}")

    def spotify_search(self, payload: str) -> None:
        if not payload.strip() or payload.strip().casefold() in ON_WORDS | OFF_WORDS:
            raise ValueError(f"spotify_search expects 'Song - Artist', got {payload!r}")
        was_running = self.spotify_process.is_running()
        title = self.spotify.play(payload)
        if not was_running and self.audio.app_mode and self.audio.pending_target:
            self.audio.apply_pending()
        self.publish_state("track", title)
        self.notify(f"Играет: {title}")


class CommandWorker(threading.Thread):
    """Executes commands one by one so slow actions never block the MQTT loop."""

    def __init__(self, router: CommandRouter) -> None:
        super().__init__(name="worker", daemon=True)
        self.router = router
        self.queue: queue.Queue[tuple[str, str] | None] = queue.Queue()

    def submit(self, topic: str, payload: str) -> None:
        self.queue.put((topic, payload))

    def stop(self) -> None:
        self.queue.put(None)

    def run(self) -> None:
        com_ready = False
        if IS_WINDOWS:
            try:  # pycaw / Core Audio needs COM initialised on this thread
                import comtypes
                comtypes.CoInitialize()
                com_ready = True
            except Exception as exc:  # noqa: BLE001
                log.warning("COM init failed: %s", exc)
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                self.router.handle_message(*item)
        finally:
            if com_ready:
                import comtypes
                comtypes.CoUninitialize()


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #

def configure_mqtt_client(client, mcfg: dict) -> None:
    """Credentials + TLS shared by the service and the --send utility."""
    if mcfg.get("username"):
        client.username_pw_set(mcfg["username"], mcfg.get("password") or None)
    if mcfg.get("tls"):
        ca = mcfg.get("ca_certs")
        client.tls_set(ca_certs=str(resolve_path(ca)) if ca else None)
        if mcfg.get("tls_insecure"):
            client.tls_insecure_set(True)


def diagnose_connection(host: str, port: int, tls: bool, ca_certs: str = "",
                        tls_insecure: bool = False, timeout: float = 5.0) -> str:
    """Probe DNS / TCP / TLS step by step; paho does not report why connect failed."""
    try:
        sock = socket.create_connection((host, port), timeout)
    except socket.gaierror as exc:
        return f"DNS lookup for '{host}' failed ({exc}) - check 'host' and the internet connection"
    except OSError as exc:
        return (f"TCP connection to {host}:{port} failed ({exc}) - check 'port', "
                "firewall/antivirus/VPN, or the broker is down")
    with sock:
        if not tls:
            return (f"TCP {host}:{port} is reachable, but the MQTT handshake failed - "
                    "if this port expects TLS, set \"tls\": true")
        ctx = ssl.create_default_context(cafile=str(resolve_path(ca_certs)) if ca_certs else None)
        if tls_insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            with ctx.wrap_socket(sock, server_hostname=host):
                pass
        except ssl.SSLCertVerificationError as exc:
            return (f"TLS certificate check failed ({exc.verify_message}) - "
                    "set 'ca_certs' or \"tls_insecure\": true")
        except (ssl.SSLError, OSError) as exc:
            return (f"TLS handshake with {host}:{port} failed ({exc}) - "
                    "this port probably does not use TLS, try \"tls\": false")
    return "TCP and TLS are OK - check username/password and that the port is an MQTT (not WebSocket) port"


class MqttService:
    """paho-mqtt 2.x client with automatic reconnect (exponential back-off)."""

    def __init__(self, cfg: dict, topics: list[str],
                 on_message: Callable[[str, str], None],
                 on_state: Callable[[str], None] = lambda _s: None) -> None:
        self.cfg = cfg["mqtt"]
        self.topics = topics
        self._on_message_cb = on_message
        self._on_state_cb = on_state
        self._client = None
        self._lock = threading.Lock()
        self._stopping = False
        self._fail_streak = 0
        self.state = "stopped"

    def _set_state(self, state: str) -> None:
        if state != self.state:
            log.info("MQTT state: %s -> %s", self.state, state)
            self.state = state
            try:
                self._on_state_cb(state)
            except Exception as exc:  # noqa: BLE001 - UI callback must not kill paho
                log.debug("State callback failed: %s", exc)

    def _build_client(self):
        import paho.mqtt.client as mqtt

        client_id = self.cfg.get("client_id") or f"alice-pc-{socket.gethostname()}".lower()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                             protocol=mqtt.MQTTv311, clean_session=True)
        configure_mqtt_client(client, self.cfg)
        client.will_set(self.cfg["status_topic"], "offline", qos=1, retain=True)
        client.reconnect_delay_set(int(self.cfg["reconnect_min_delay"]),
                                   int(self.cfg["reconnect_max_delay"]))
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def start(self) -> None:
        with self._lock:
            self._stopping = False
            self._client = self._build_client()
            host, port = self.cfg["host"], int(self.cfg["port"])
            log.info("Connecting to MQTT broker %s:%s (tls=%s)", host, port, bool(self.cfg.get("tls")))
            self._set_state("connecting")
            # connect_async + loop_start: paho's network thread keeps retrying forever,
            # also when the network / broker is not reachable yet at Windows logon.
            self._client.connect_async(host, port, int(self.cfg["keepalive"]))
            self._client.loop_start()

    def stop(self) -> None:
        with self._lock:
            client, self._client = self._client, None
            if client is None:
                return
            self._stopping = True
            try:
                if self.state == "connected":
                    client.publish(self.cfg["status_topic"], "offline", qos=1,
                                   retain=True).wait_for_publish(timeout=2)
                client.disconnect()
            except Exception as exc:  # noqa: BLE001
                log.debug("MQTT disconnect error: %s", exc)
            client.loop_stop()
            self._set_state("stopped")

    def restart(self) -> None:
        log.info("MQTT restart requested")
        self.stop()
        self.start()

    def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        client = self._client
        if client is not None and self.state == "connected":
            client.publish(topic, payload, qos=int(self.cfg["qos"]), retain=retain)

    # -- paho callbacks (run on paho's network thread) ---------------------- #
    def _on_connect(self, client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code.is_failure:
            log.error("MQTT connection refused by broker: %s", reason_code)
            self._set_state("disconnected")
            return
        qos = int(self.cfg["qos"])
        client.subscribe([(t, qos) for t in self.topics])  # re-subscribe after every reconnect
        client.publish(self.cfg["status_topic"], "online", qos=1, retain=True)
        log.info("Subscribed to: %s", ", ".join(self.topics))
        self._fail_streak = 0
        self._set_state("connected")

    def _on_connect_fail(self, _client, _userdata) -> None:
        log.warning("MQTT broker unreachable, retrying with back-off (max %ss)",
                    self.cfg["reconnect_max_delay"])
        if self._fail_streak % 10 == 0:  # explain the first failure, then every 10th
            log.warning("MQTT diagnosis: %s", diagnose_connection(
                self.cfg["host"], int(self.cfg["port"]), bool(self.cfg.get("tls")),
                self.cfg.get("ca_certs", ""), bool(self.cfg.get("tls_insecure"))))
        self._fail_streak += 1
        self._set_state("disconnected")

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        if self._stopping:
            return
        log.warning("MQTT connection lost (%s), auto-reconnecting", reason_code)
        self._set_state("disconnected")

    def _on_message(self, _client, _userdata, msg) -> None:
        payload = msg.payload.decode("utf-8", errors="replace")
        if msg.retain and self.cfg.get("ignore_retained", True):
            log.info("Ignoring retained (old) message on %s: %r", msg.topic, payload)
            return
        log.info("MQTT <- %s: %r", msg.topic, payload)
        self._on_message_cb(msg.topic, payload)


# --------------------------------------------------------------------------- #
# Application + tray icon
# --------------------------------------------------------------------------- #

STATE_COLORS = {"connected": (30, 185, 84), "connecting": (240, 170, 0),
                "disconnected": (220, 50, 50), "stopped": (128, 128, 128)}
STATE_LABELS = {"connected": "подключено", "connecting": "подключение...",
                "disconnected": "нет связи, переподключение", "stopped": "остановлено"}


def make_icon_image(state: str):
    """64x64 round icon with a 'play' triangle; colour reflects the MQTT state."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 2, size - 2), fill=STATE_COLORS.get(state, (128, 128, 128)))
    draw.polygon([(24, 17), (24, 47), (48, 32)], fill=(255, 255, 255))
    return img


class Application:
    """Wires config, worker, MQTT and the tray icon together."""

    def __init__(self, cfg: dict, config_path: Path, log_file: Path, use_tray: bool) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.log_file = log_file
        self.use_tray = use_tray
        self.icon = None
        self.router = CommandRouter(cfg, publish_state=self.publish_state, notify=self.notify)
        self.worker = CommandWorker(self.router)
        self.mqtt = MqttService(cfg, list(self.router.routes),
                                on_message=self.worker.submit, on_state=self._on_mqtt_state)
        self._stop_event = threading.Event()
        self._settings_lock = threading.Lock()
        self.restart_requested = False

    # -- callbacks --------------------------------------------------------- #
    def publish_state(self, name: str, value: str) -> None:
        prefix = self.cfg["mqtt"]["state_prefix"].rstrip("/")
        self.mqtt.publish(f"{prefix}/{name}", value, retain=(name != "error"))

    def notify(self, message: str) -> None:
        if self.icon is not None and self.cfg["tray"].get("notifications", True):
            try:
                self.icon.notify(message, APP_NAME)
            except Exception as exc:  # noqa: BLE001
                log.debug("Tray notification failed: %s", exc)

    def _on_mqtt_state(self, state: str) -> None:
        if self.icon is not None:
            self.icon.icon = make_icon_image(state)
            self.icon.title = f"{APP_NAME}: MQTT {STATE_LABELS.get(state, state)}"
            self.icon.update_menu()

    # -- lifecycle --------------------------------------------------------- #
    def open_settings(self) -> None:
        if not self._settings_lock.acquire(blocking=False):
            return  # already open

        def run() -> None:
            try:
                import settings_ui
                settings_ui.open_settings(self.config_path)  # saving -> _watch_config restarts us
            except Exception:
                log.exception("Settings window failed")
            finally:
                self._settings_lock.release()

        threading.Thread(target=run, name="settings", daemon=True).start()

    def _watch_config(self) -> None:
        """Restart (tray mode) when config.json is saved, so new settings apply at once."""
        def mtime():
            try:
                return self.config_path.stat().st_mtime_ns
            except OSError:
                return None

        last = mtime()
        while not self._stop_event.wait(2):
            current = mtime()
            if current == last:
                continue
            last = current
            try:
                load_config(self.config_path)
            except (OSError, ValueError) as exc:
                log.warning("config.json changed but is invalid, not applying it: %s", exc)
                continue
            log.info("config.json changed - restarting to apply it")
            self.restart_requested = True
            self.shutdown()
            if self.icon is not None:
                self.icon.stop()
            return

    def _start_services(self) -> None:
        self.worker.start()
        self.mqtt.start()
        if self.use_tray:
            threading.Thread(target=self._watch_config, name="config-watch", daemon=True).start()
        if not self.router.spotify.use_web_api():
            log.info("Track search: keyless mode (DuckDuckGo + spotify: links, no Premium needed)")

    def shutdown(self) -> None:
        if self._stop_event.is_set():
            return
        log.info("Shutting down")
        self.mqtt.stop()
        self.worker.stop()
        self.worker.join(timeout=5)
        self._stop_event.set()

    def run(self) -> None:
        log.info("%s %s starting (config: %s)", APP_NAME, APP_VERSION, self.config_path)
        if self.use_tray:
            self._run_tray()
        else:
            self._run_console()
        log.info("Stopped")

    def _run_console(self) -> None:
        self._start_services()
        log.info("Console mode - press Ctrl+C to quit")
        try:
            while not self._stop_event.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def _run_tray(self) -> None:
        import pystray
        from pystray import Menu, MenuItem

        def open_path(path: Path):
            return lambda *_: os.startfile(str(path))

        def on_quit(icon, _item):
            self.shutdown()
            icon.stop()

        def spotify_label(_item) -> str:
            return "Spotify API: " + ("авторизован" if self.router.spotify.is_authorized()
                                      else "нужен вход (--auth)")

        def in_thread(func):
            return lambda *_: threading.Thread(target=func, daemon=True).start()

        menu = Menu(
            MenuItem(lambda _i: f"MQTT: {STATE_LABELS.get(self.mqtt.state, self.mqtt.state)}",
                     None, enabled=False),
            MenuItem(spotify_label, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Настройки...", lambda *_: self.open_settings(), default=True),
            MenuItem("Показать Spotify", in_thread(self.router.spotify_process.show_window)),
            MenuItem("Спрятать Spotify в трей", in_thread(self.router.spotify_process.hide_window)),
            Menu.SEPARATOR,
            MenuItem("Переподключить MQTT", lambda *_: threading.Thread(
                target=self.mqtt.restart, name="mqtt-restart", daemon=True).start()),
            MenuItem("Открыть лог", open_path(self.log_file)),
            MenuItem("Открыть config.json", open_path(self.config_path)),
            MenuItem("Открыть папку программы", open_path(BASE_DIR)),
            Menu.SEPARATOR,
            MenuItem("Выход", on_quit),
        )
        self.icon = pystray.Icon("alice_pc", make_icon_image("stopped"),
                                 f"{APP_NAME}: запуск...", menu)

        def setup(icon):
            icon.visible = True
            self._start_services()

        self.icon.run(setup=setup)  # blocks the main thread until "Выход"


# --------------------------------------------------------------------------- #
# CLI utilities
# --------------------------------------------------------------------------- #

def cmd_auth(cfg: dict, manual: bool) -> int:
    """Interactive one-time Spotify login; stores the refresh token in the cache file."""
    import spotipy
    oauth = build_spotify_oauth(cfg, interactive=True, open_browser=not manual)
    print("Spotify authorization")
    print(f"  Redirect URI (must match the Developer Dashboard exactly): {cfg['spotify']['redirect_uri']}")
    if manual:
        print("  Manual mode: open the link, log in, then copy the FULL address from the browser\n"
              "  address bar (even if the page says 'site can't be reached') and paste it here.")
    else:
        print("  A browser window opens. After you click 'Agree' the code is captured\n"
              "  automatically by a temporary local server on the redirect URI port.")
    sp = spotipy.Spotify(auth_manager=oauth, requests_timeout=15)
    user = sp.current_user()
    print(f"\nOK: logged in as {user.get('display_name') or user.get('id')} "
          f"(product: {user.get('product', 'unknown')})")
    print(f"Token cache: {resolve_path(cfg['spotify']['cache_path'])}")
    if user.get("product") not in (None, "premium"):
        print("WARNING: playback control through the Web API requires Spotify Premium.")
    devices = sp.devices().get("devices", [])
    print("Spotify Connect devices visible now:",
          ", ".join(f"{d['name']} ({d['type']})" for d in devices) or "none (start Spotify to see it)")
    return 0


def cmd_list_devices(cfg: dict) -> int:
    switcher = AudioSwitcher(cfg)
    devices = switcher.list_devices()
    print(f"Active playback devices (backend: {switcher.backend.name}):")
    if not devices:
        print("  (none found)")
    for dev in devices:
        print(f"  {'*' if dev.is_default else ' '} {dev.name}")
    print("\n'*' = current default. Copy a name (or a unique part of it) into config.json -> audio.devices\n")
    print("Current config.json mapping:")
    for target, wanted in switcher.mapping().items():
        match = AudioSwitcher.match(devices, wanted)
        print(f"  {target:<12} {wanted} -> {match.name if match else 'NOT FOUND'}")
    return 0


def cmd_send(cfg: dict, topic: str, payload: str) -> int:
    """Publish one message through the configured broker (end-to-end test)."""
    import paho.mqtt.client as mqtt
    mcfg = cfg["mqtt"]
    connected = threading.Event()
    result: dict[str, Any] = {"rc": None}

    def on_connect(_client, _u, _f, reason_code, _p):
        result["rc"] = reason_code
        connected.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"alice-pc-send-{os.getpid()}", protocol=mqtt.MQTTv311)
    configure_mqtt_client(client, mcfg)
    client.on_connect = on_connect
    client.connect(mcfg["host"], int(mcfg["port"]), 30)
    client.loop_start()
    try:
        if not connected.wait(15) or result["rc"].is_failure:
            print(f"Failed to connect to the broker (result: {result['rc']})", file=sys.stderr)
            if result["rc"] is None:
                print("Diagnosis: " + diagnose_connection(
                    mcfg["host"], int(mcfg["port"]), bool(mcfg.get("tls")), mcfg.get("ca_certs", ""),
                    bool(mcfg.get("tls_insecure"))), file=sys.stderr)
            return 1
        info = client.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=10)   # wait here, never inside a paho callback
        if not info.is_published():
            print("Broker did not acknowledge the message", file=sys.stderr)
            return 1
    finally:
        client.disconnect()
        client.loop_stop()
    print(f"Published {payload!r} to {topic}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="path to config.json")
    parser.add_argument("--no-tray", action="store_true", help="run in the console without a tray icon")
    parser.add_argument("--auth", action="store_true", help="one-time Spotify login (opens a browser)")
    parser.add_argument("--auth-manual", action="store_true",
                        help="Spotify login by pasting the redirect URL (no local server)")
    parser.add_argument("--list-devices", action="store_true", help="list Windows playback devices")
    parser.add_argument("--settings", action="store_true", help="open the settings window")
    parser.add_argument("--test", nargs=2, metavar=("COMMAND", "PAYLOAD"),
                        help=f"run one command locally, COMMAND = {' | '.join(COMMANDS)}")
    parser.add_argument("--send", nargs=2, metavar=("TOPIC", "PAYLOAD"),
                        help="publish a message to the configured MQTT broker")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = args.config.resolve()
    if args.settings or not config_path.exists():
        import settings_ui
        saved = settings_ui.open_settings(config_path, first_run=not config_path.exists())
        if args.settings or not saved:
            return 0
    try:
        cfg = load_config(config_path)
    except (OSError, ValueError) as exc:
        show_error_box(str(exc))
        return 2

    utility_mode = bool(args.auth or args.auth_manual or args.list_devices or args.test or args.send)
    log_file = setup_logging(cfg, console=args.no_tray or utility_mode)
    if utility_mode and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    try:
        if args.auth or args.auth_manual:
            return cmd_auth(cfg, manual=args.auth_manual)
        if args.list_devices:
            return cmd_list_devices(cfg)
        if args.send:
            return cmd_send(cfg, *args.send)
        if args.test:
            command, payload = args.test
            if command not in COMMANDS:
                print(f"Unknown command {command!r}. Use one of: {', '.join(COMMANDS)}", file=sys.stderr)
                return 2
            return 0 if CommandRouter(cfg).execute(command, payload) else 1
    except Exception as exc:
        log.error("%s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
        return 1

    if not cfg["mqtt"].get("host"):
        import settings_ui
        if settings_ui.open_settings(config_path, first_run=True):
            cfg = load_config(config_path)
        if not cfg["mqtt"].get("host"):
            show_error_box(f"mqtt.host is empty. Fill in the broker settings in:\n{config_path}")
            return 2

    instance = SingleInstance()
    if instance.already_running:
        show_error_box(f"{APP_NAME} is already running (see the tray icon).")
        return 3

    app = Application(cfg, config_path, log_file, use_tray=not args.no_tray)
    app.run()
    if app.restart_requested:
        instance.release()
        subprocess.Popen([sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]],
                         cwd=str(BASE_DIR), close_fds=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
