#!/usr/bin/env python3
"""Single-file voice-tunnel agent (stdlib only).

Runs on Ubuntu X11 and Windows. It connects to the voice-tunnel server,
accepts ``insert_text`` messages, injects text into the focused window,
and exposes a loopback HTTP endpoint for Deskflow enter/leave events.

Usage:
    python3 voice_tunnel_agent.py --config config.ini --host yuchen-X99
"""

from __future__ import annotations

import argparse
import collections
import configparser
import ctypes
import json
import logging
import math
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PROTOCOL_VERSION = 1
MAX_LINE = 1024 * 1024
LOG = logging.getLogger("voice_tunnel.agent")


# --------------------------------------------------------------------------- config


@dataclass
class Options:
    server: str = "192.168.2.134"
    port: int = 8765
    token: str = "change-me"
    host: str = ""
    local_port: int = 8766
    hud: bool = True
    dry_run: bool = False
    allow_clipboard_fallback: bool = False
    inject_mode: str = "auto"
    terminal_classes: str = "gnome-terminal-server,gnome-terminal,konsole,xterm,alacritty,kitty,wezterm,terminator,tilix"
    font: str = ""
    max_chars: int = 240
    offset_x: int = 18
    offset_y: int = 22


def _cfg_get(parser: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    if parser.has_section(section) and parser.has_option(section, key):
        return parser.get(section, key)
    return default


def _cfg_bool(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    return _cfg_get(parser, section, key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def load_options(args: argparse.Namespace) -> Options:
    parser = configparser.ConfigParser()
    path = Path(args.config) if args.config else None
    if path and path.exists():
        parser.read(path, encoding="utf-8")
    default_font = "Microsoft YaHei UI" if os.name == "nt" else "Noto Sans CJK SC"
    opts = Options(
        server=_cfg_get(parser, "agent", "server", "192.168.2.134"),
        port=int(_cfg_get(parser, "agent", "port", "8765")),
        token=_cfg_get(parser, "agent", "token", "change-me"),
        host=_cfg_get(parser, "agent", "host", platform.node()),
        local_port=int(_cfg_get(parser, "agent", "local_port", "8766")),
        hud=_cfg_bool(parser, "agent", "hud", True),
        allow_clipboard_fallback=_cfg_bool(parser, "agent", "allow_clipboard_fallback", False),
        inject_mode=_cfg_get(parser, "agent", "inject_mode", "auto"),
        terminal_classes=_cfg_get(parser, "agent", "terminal_classes", "gnome-terminal-server,gnome-terminal,konsole,xterm,alacritty,kitty,wezterm,terminator,tilix"),
        font=_cfg_get(parser, "hud", "font_linux" if os.name != "nt" else "font_windows", default_font),
        max_chars=int(_cfg_get(parser, "hud", "max_chars", "240")),
        offset_x=int(_cfg_get(parser, "hud", "offset_x", "18")),
        offset_y=int(_cfg_get(parser, "hud", "offset_y", "22")),
    )
    for name in ("server", "port", "token", "host", "local_port"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(opts, name, value)
    if getattr(args, "hud", None) is not None:
        opts.hud = bool(args.hud)
    if getattr(args, "dry_run", False):
        opts.dry_run = True
    if getattr(args, "allow_clipboard_fallback", False):
        opts.allow_clipboard_fallback = True
    if getattr(args, "inject_mode", None):
        opts.inject_mode = args.inject_mode
    return opts


# --------------------------------------------------------------------------- helper


def encode_message(message: dict[str, Any]) -> bytes:
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_LINE:
        raise ValueError("message too large")
    return data + b"\n"


def read_line(sock: socket.socket, buffer: bytearray) -> dict[str, Any]:
    while b"\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("server closed connection")
        buffer.extend(chunk)
    line, _, rest = buffer.partition(b"\n")
    buffer[:] = rest
    if len(line) > MAX_LINE:
        raise ValueError("message too large")
    message = json.loads(line.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("invalid message")
    return message


class Dedupe:
    def __init__(self, maxlen: int = 512) -> None:
        self._items: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._maxlen = maxlen
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        if not key:
            return False
        with self._lock:
            if key in self._items:
                return True
            self._items[key] = time.time()
            while len(self._items) > self._maxlen:
                self._items.popitem(last=False)
            return False


# --------------------------------------------------------------------------- injection


class Injector:
    def __init__(self, options: Options) -> None:
        self.options = options

    def inject(self, text: str) -> str:
        if self.options.dry_run:
            LOG.info("dry-run inject %d chars: %s", len(text), text[:80])
            return "dry-run"
        if os.name == "nt":
            self._inject_windows(text)
            return "windows-sendinput"
        return self._inject_linux(text)

    # -- Windows ---------------------------------------------------------

    def _inject_windows(self, text: str) -> None:
        import ctypes
        from ctypes import wintypes

        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        raw = text.encode("utf-16-le")
        if len(raw) % 2:
            raw += b"\x00"
        for i in range(0, len(raw), 2):
            unit = int.from_bytes(raw[i : i + 2], "little")
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                inp = INPUT(
                    type=INPUT_KEYBOARD,
                    union=INPUTUNION(ki=KEYBDINPUT(wVk=0, wScan=unit, dwFlags=flags, time=0, dwExtraInfo=0)),
                )
                if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
                    raise RuntimeError(f"SendInput failed (error={ctypes.get_last_error()})")
            time.sleep(0.001)

    # -- Linux X11 -------------------------------------------------------

    def _inject_linux(self, text: str) -> str:
        mode = self.options.inject_mode
        non_ascii = any(ord(ch) > 127 for ch in text)
        if mode == "clipboard" or (
            mode == "auto" and self.options.allow_clipboard_fallback and non_ascii
        ):
            return self._inject_linux_clipboard(text)
        if shutil.which("xdotool"):
            result = subprocess.run(
                ["xdotool", "type", "--clearmodifiers", "--delay", "30", text],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                return "xdotool"
            LOG.warning("xdotool failed: %s", (result.stderr or result.stdout).strip())
            if not self.options.allow_clipboard_fallback:
                raise RuntimeError("xdotool type failed; enable clipboard fallback to try paste")
        if self.options.allow_clipboard_fallback:
            return self._inject_linux_clipboard(text)
        raise RuntimeError("xdotool not installed (install it or enable allow_clipboard_fallback)")

    def _active_window_class(self) -> str:
        try:
            wid = subprocess.run(
                ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if wid and shutil.which("xprop"):
                prop = subprocess.run(
                    ["xprop", "-id", wid, "WM_CLASS"], capture_output=True, text=True, timeout=2
                ).stdout
                parts = re.findall(r'"([^"]*)"', prop)
                if parts:
                    return parts[-1].lower()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            title = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=2
            ).stdout.lower()
            return title
        except (OSError, subprocess.SubprocessError):
            return ""

    def _paste_chord(self) -> str:
        window_class = self._active_window_class()
        terminals = [item.strip().lower() for item in self.options.terminal_classes.split(",") if item.strip()]
        if any(name in window_class for name in terminals):
            return "ctrl+shift+v"
        return "ctrl+v"

    def _inject_linux_clipboard(self, text: str) -> str:
        if not shutil.which("xclip") or not shutil.which("xdotool"):
            raise RuntimeError("clipboard fallback needs xclip and xdotool")
        old = ""
        try:
            saved = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=3
            )
            if saved.returncode == 0:
                old = saved.stdout
        except (OSError, subprocess.SubprocessError):
            old = ""
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True, timeout=5)
        time.sleep(0.10)
        try:
            check = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=3
            )
            if check.stdout.rstrip("\n") != text.rstrip("\n"):
                LOG.warning("clipboard verification mismatch (%d vs %d chars)", len(check.stdout), len(text))
        except (OSError, subprocess.SubprocessError):
            pass
        chord = self._paste_chord()
        try:
            subprocess.run(["xdotool", "key", "--clearmodifiers", chord], check=True, timeout=5)
            time.sleep(1.0)
        finally:
            if old:
                try:
                    subprocess.run(
                        ["xclip", "-selection", "clipboard"], input=old, text=True, timeout=5
                    )
                except (OSError, subprocess.SubprocessError):
                    LOG.warning("failed to restore previous clipboard text")
        return f"clipboard:{chord}"


# --------------------------------------------------------------------------- caret


class CaretTracker(threading.Thread):
    """Best-effort caret position; the HUD falls back to the mouse."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="caret-tracker")
        self._pos: tuple[int, int] | None = None
        self._ts = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def get(self) -> tuple[int, int] | None:
        with self._lock:
            if self._pos and time.time() - self._ts < 1.2:
                return self._pos
        return None

    def run(self) -> None:
        while not self._stop.is_set():
            pos = self._query()
            if pos:
                with self._lock:
                    self._pos = pos
                    self._ts = time.time()
            self._stop.wait(0.4)

    def stop(self) -> None:
        self._stop.set()

    def _query(self) -> tuple[int, int] | None:
        try:
            if os.name == "nt":
                return self._windows_caret()
            return self._linux_caret()
        except Exception:  # noqa: BLE001 - caret APIs are best effort
            return None

    def _windows_caret(self) -> tuple[int, int] | None:
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", RECT),
            ]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        cur = kernel32.GetCurrentThreadId()
        attached = bool(user32.AttachThreadInput(cur, tid, True))
        try:
            info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
            if not user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
                return None
            if not info.hwndCaret:
                return None
            point = wintypes.POINT()
            if not user32.GetCaretPos(ctypes.byref(point)):
                return None
            user32.ClientToScreen(info.hwndCaret, ctypes.byref(point))
            return int(point.x), int(point.y + 18)
        finally:
            if attached:
                user32.AttachThreadInput(cur, tid, False)

    def _linux_caret(self) -> tuple[int, int] | None:
        # pyatspi 在无 AT-SPI 总线的机器上不仅拿不到光标，还会与 GTK 的
        # atk-bridge 冲突导致 libatspi segfault（2026-11-11 x670 实测把整个
        # Agent 崩掉、active_target 失联）。光标跟随一律回退到指针位置。
        return None


class X11Focus:
    """Save/restore X11 input focus so the HUD never steals keyboard focus."""

    def __init__(self) -> None:
        self._lib: Any = None
        self._dpy: Any = None
        if platform.system() != "Linux" or not os.environ.get("DISPLAY"):
            return
        try:
            self._lib = ctypes.CDLL("libX11.so.6")
            self._lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self._lib.XOpenDisplay.restype = ctypes.c_void_p
            self._dpy = self._lib.XOpenDisplay(None)
            self._lib.XGetInputFocus.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int),
            ]
            self._lib.XGetInputFocus.restype = ctypes.c_int
            self._lib.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            self._lib.XSetInputFocus.restype = ctypes.c_int
            self._lib.XFlush.argtypes = [ctypes.c_void_p]
            self._lib.XFlush.restype = ctypes.c_int
        except Exception:
            self._lib = None
            self._dpy = None

    def get(self) -> int | None:
        if not self._lib or not self._dpy:
            return None
        try:
            win = ctypes.c_ulong(0)
            revert = ctypes.c_int(0)
            self._lib.XGetInputFocus(self._dpy, ctypes.byref(win), ctypes.byref(revert))
            return int(win.value) or None
        except Exception:
            return None

    def restore(self, win: int | None) -> None:
        if not win or not self._lib or not self._dpy:
            return
        try:
            self._lib.XSetInputFocus(self._dpy, ctypes.c_ulong(win), 2, 0)
            self._lib.XFlush(self._dpy)
        except Exception:
            pass

    def focused_geometry(self) -> tuple[int, int, int, int] | None:
        """聚焦窗口（回溯到顶层）在根坐标系下的 (x, y, w, h)，供 HUD 锚定。"""
        if not self._lib or not self._dpy:
            return None
        try:
            win = self.get()
            if not win or win <= 2:  # 0/1 是特殊值（None/PointerRoot）
                return None

            self._lib.XQueryTree.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                ctypes.POINTER(ctypes.c_uint),
            ]
            self._lib.XQueryTree.restype = ctypes.c_int
            self._lib.XFree.argtypes = [ctypes.c_void_p]
            self._lib.XFree.restype = None

            root_return = ctypes.c_ulong(0)
            # 沿父链爬到顶层窗口（输入焦点常落在深层子控件上）
            for _ in range(16):
                parent_return = ctypes.c_ulong(0)
                children = ctypes.POINTER(ctypes.c_ulong)()
                n = ctypes.c_uint(0)
                if not self._lib.XQueryTree(
                    self._dpy, ctypes.c_ulong(win), ctypes.byref(root_return),
                    ctypes.byref(parent_return), ctypes.byref(children), ctypes.byref(n),
                ):
                    return None
                if children:
                    self._lib.XFree(children)
                if parent_return.value == 0 or parent_return.value == root_return.value:
                    break
                win = parent_return.value

            class XWindowAttributes(ctypes.Structure):
                _fields_ = [
                    ("x", ctypes.c_int),
                    ("y", ctypes.c_int),
                    ("width", ctypes.c_uint),
                    ("height", ctypes.c_uint),
                    ("border_width", ctypes.c_uint),
                    ("depth", ctypes.c_uint),
                    ("visual", ctypes.c_void_p),
                    ("root", ctypes.c_ulong),
                    ("class", ctypes.c_uint),
                    ("bit_gravity", ctypes.c_uint),
                    ("win_gravity", ctypes.c_uint),
                    ("backing_store", ctypes.c_uint),
                    ("backing_planes", ctypes.c_ulong),
                    ("backing_pixel", ctypes.c_ulong),
                    ("save_under", ctypes.c_int),
                    ("colormap", ctypes.c_ulong),
                    ("map_installed", ctypes.c_int),
                    ("map_state", ctypes.c_uint),
                    ("all_event_masks", ctypes.c_ulong),
                    ("your_event_mask", ctypes.c_ulong),
                    ("do_not_propagate_mask", ctypes.c_ulong),
                    ("override_redirect", ctypes.c_int),
                    ("screen", ctypes.c_void_p),
                ]

            self._lib.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(XWindowAttributes)]
            self._lib.XGetWindowAttributes.restype = ctypes.c_int
            self._lib.XTranslateCoordinates.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong),
            ]
            self._lib.XTranslateCoordinates.restype = ctypes.c_int

            attrs = XWindowAttributes()
            if not self._lib.XGetWindowAttributes(self._dpy, ctypes.c_ulong(win), ctypes.byref(attrs)):
                return None
            rx = ctypes.c_int(0)
            ry = ctypes.c_int(0)
            child = ctypes.c_ulong(0)
            if not self._lib.XTranslateCoordinates(
                self._dpy, ctypes.c_ulong(win), attrs.root, 0, 0, ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child)
            ):
                return None
            if attrs.map_state != 2 or attrs.width < 80 or attrs.height < 60:  # IsUnmapped / 太小
                return None
            # 注意：Python 3.10 下 int(c_int) 会把原始字节串当十进制文本解析而报错，必须取 .value
            return int(rx.value), int(ry.value), int(attrs.width), int(attrs.height)
        except Exception:
            return None


# --------------------------------------------------------------------------- HUD


class Hud:
    PAD_X = 12
    PAD_Y = 8
    MAX_WIDTH = 420
    MAX_LINES = 6

    def __init__(self, options: Options, tracker: CaretTracker | None) -> None:
        self.options = options
        self.tracker = tracker
        self.queue: queue.Queue[tuple[str, str, int]] = queue.Queue()
        self.root: Any = None
        self.win: Any = None
        self.text: Any = None
        self._hide_job: str | None = None
        self._last_text = ""
        self._size = (220, 40)
        self._focus = X11Focus()
        self._saved_focus: int | None = None

    def submit(self, kind: str, text: str, stable_len: int = -1) -> None:
        self.queue.put((kind, text, stable_len))

    def start(self) -> bool:
        import tkinter as tk

        try:
            self._saved_focus = self._focus.get()
            self.root = tk.Tk()
            self.root.withdraw()
            self.win = tk.Toplevel(self.root)
            self.win.withdraw()
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            try:
                self.win.attributes("-alpha", 0.86)
            except Exception:
                pass
            self.text = tk.Text(
                self.win,
                wrap="char",
                bg="#ffffff",
                fg="#111827",
                insertwidth=0,
                borderwidth=0,
                highlightthickness=1,
                highlightbackground="#d5dce8",
                highlightcolor="#d5dce8",
                padx=self.PAD_X,
                pady=self.PAD_Y,
                font=(self.options.font, 11),
                spacing1=1,
                spacing3=1,
                cursor="arrow",
                takefocus=0,
                relief="flat",
                width=1,
                height=1,
            )
            self.text.pack(side="left", fill="both", expand=True)
            self.text.bind("<Button-4>", lambda _event: self.text.yview_scroll(-1, "units"))
            self.text.bind("<Button-5>", lambda _event: self.text.yview_scroll(1, "units"))
            self.text.bind("<MouseWheel>", lambda event: self.text.yview_scroll(-1 if event.delta > 0 else 1, "units"))
            self.text.tag_configure("stable", foreground="#111827")
            self.text.tag_configure("candidate", foreground="#93a1b5")
            self.text.configure(state="disabled")
            self.win.update_idletasks()
            self._set_no_activate()
            self._focus.restore(self._saved_focus)
            self.root.after(40, self._poll)
            return True
        except Exception as exc:  # noqa: BLE001 - HUD is optional
            LOG.warning("HUD disabled: %s", exc)
            self.root = None
            return False

    def _set_no_activate(self) -> None:
        if os.name != "nt":
            try:
                self.win.wm_attributes("-type", "utility")
            except Exception:
                pass
            return
        try:
            hwnd = self.win.winfo_id()
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def _poll(self) -> None:
        dirty = False
        while True:
            try:
                kind, text, stable_len = self.queue.get_nowait()
            except queue.Empty:
                break
            dirty = True
            self._render(kind, text, stable_len)
        if dirty:
            self._place()
        if self.root is not None:
            self.root.after(40, self._poll)

    def _render(self, kind: str, text: str, stable_len: int) -> None:
        if self.text is None:
            return
        text = text[-self.options.max_chars :]
        if not text or kind == "hide":
            self._cancel_hide()
            self.hide()
            return
        self._cancel_hide()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        if kind == "partial" and 0 <= stable_len < len(text):
            self.text.tag_add("stable", "1.0", f"1.0+{stable_len}c")
            self.text.tag_add("candidate", f"1.0+{stable_len}c", "end")
        else:
            self.text.tag_add("stable", "1.0", "end")
        self.text.configure(state="disabled")
        import tkinter.font as tkfont

        font = tkfont.Font(font=self.text.cget("font"))
        usable = self.MAX_WIDTH - 2 * self.PAD_X - 10
        paragraphs = text.splitlines() or [""]
        widest = max((font.measure(line) for line in paragraphs), default=0)
        total_lines = sum(max(1, math.ceil(font.measure(line) / usable)) for line in paragraphs)
        lines = max(1, min(self.MAX_LINES, total_lines))
        width = min(self.MAX_WIDTH, max(200, min(widest, usable) + 2 * self.PAD_X + 6))
        line_h = font.metrics("linespace") + 2
        self._size = (width, lines * line_h + 2 * self.PAD_Y + 2)
        self.text.see("end" if total_lines > self.MAX_LINES else "1.0")
        saved = self._focus.get()
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        self._focus.restore(saved)
        if kind == "final":
            self._hide_job = self.root.after(1200, self.hide)
        else:
            # 无后续更新时自动消失，避免注入失败/目标切换后候选框滞留
            self._hide_job = self.root.after(6000, self.hide)

    def _cancel_hide(self) -> None:
        if self._hide_job:
            try:
                self.root.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

    def _place(self) -> None:
        if self.win is None:
            return
        w, h = self._size
        anchor = self.tracker.get() if self.tracker else None
        if anchor is not None and anchor[0] <= 0 and anchor[1] <= 0:
            anchor = None
        if anchor is None and self.root is not None:
            anchor = (self.root.winfo_pointerx(), self.root.winfo_pointery())
        if anchor is None:
            return
        ax, ay = anchor
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = ax + self.options.offset_x
        y = ay + self.options.offset_y
        if x + w > sw:
            x = max(0, ax - w - self.options.offset_x)
        if y + h > sh:
            y = max(0, ay - h - self.options.offset_y)
        self.win.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

    def hide(self) -> None:
        if self.win is not None:
            self.win.withdraw()

    def run(self) -> None:
        if self.root is not None:
            self.root.mainloop()

    def stop(self) -> None:
        if self.root is not None:
            try:
                self.root.quit()
            except Exception:
                pass


class GtkHud:
    """输入法风格的候选浮窗（GTK3 + cairo：圆角、阴影、RGBA 半透明）。"""

    SHADOW = 12
    RADIUS = 14
    PAD_X = 18
    PAD_Y = 14
    DOT_W = 14          # 说话指示点占用的宽度
    MAX_W = 440
    MAX_LINES = 6
    FG = (0x1F / 255, 0x23 / 255, 0x29 / 255, 1.0)
    FG_DIM = (0x94 / 255, 0xA3 / 255, 0xB8 / 255, 1.0)
    CARD = (1.0, 1.0, 1.0, 0.97)
    BORDER = (0x0F / 255, 0x17 / 255, 0x2A / 255, 0.10)
    ACCENT = (0x25 / 255, 0x63 / 255, 0xEB / 255, 1.0)

    def __init__(self, options: Options, tracker: CaretTracker | None) -> None:
        self.options = options
        self.tracker = tracker
        self.queue: queue.Queue[tuple[str, str, int]] = queue.Queue()
        self.win: Any = None
        self._text = ""
        self._stable = -1
        self._kind = "partial"
        self._hide_job: Any = None
        self._rgba = False
        self._cairo: Any = None
        self._focus = X11Focus()
        self._saved_focus: int | None = None
        self._Gtk = self._Gdk = self._GLib = self._Pango = None

    def submit(self, kind: str, text: str, stable_len: int = -1) -> None:
        self.queue.put((kind, text, stable_len))

    def start(self) -> bool:
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            from gi.repository import GLib, Gdk, Gtk, Pango, PangoCairo

            self._Gtk, self._Gdk, self._GLib, self._Pango, self._PangoCairo = Gtk, Gdk, GLib, Pango, PangoCairo
        except Exception as exc:  # noqa: BLE001
            LOG.warning("GTK HUD unavailable: %s", exc)
            return False
        try:
            import cairo

            self._cairo = cairo
        except Exception:  # noqa: BLE001
            self._cairo = None

        try:
            self._saved_focus = self._focus.get()
        except Exception:  # noqa: BLE001
            self._saved_focus = None

        Gtk.Window.set_default_icon_name("voice-tunnel")
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("voice-tunnel-hud")
        win.set_decorated(False)
        win.set_resizable(False)
        win.set_app_paintable(True)
        win.set_keep_above(True)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            win.set_visual(visual)
            self._rgba = True
        win.connect("draw", self._draw)
        win.connect("delete-event", lambda *_: self.hide() or True)
        self.win = win
        self._GLib.timeout_add(40, self._poll)
        LOG.info("HUD backend: gtk3 (rgba=%s)", self._rgba)
        return True

    def run(self) -> None:
        if self.win is not None:
            self._Gtk.main()

    def stop(self) -> None:
        if self.win is not None:
            self._GLib.idle_add(self._Gtk.main_quit)

    # ------------------------------------------------------------------ queue

    def _poll(self) -> bool:
        dirty = False
        while True:
            try:
                kind, text, stable = self.queue.get_nowait()
            except queue.Empty:
                break
            dirty = True
            self._render(kind, text, stable)
        return True  # keep the timer alive

    def _cancel_hide(self) -> None:
        if self._hide_job is not None:
            self._GLib.source_remove(self._hide_job)
            self._hide_job = None

    def _hide(self) -> None:
        if self.win is not None:
            self.win.hide()

    # ------------------------------------------------------------------ render

    def _render(self, kind: str, text: str, stable_len: int) -> None:
        if self.win is None:
            return
        self._cancel_hide()
        text = text[-self.options.max_chars :]
        if not text or kind == "hide":
            self._hide()
            return
        self._kind = kind
        self._text = text
        self._stable = stable_len
        w, h = self._measure()
        anchor = self._anchor()
        if anchor is None:
            return
        sw = self.win.get_screen().get_width()
        sh = self.win.get_screen().get_height()
        x = int(anchor[0]) + self.options.offset_x
        y = int(anchor[1]) + self.options.offset_y
        if x + w > sw - 8:
            x = max(8, int(anchor[0]) - w - self.options.offset_x)
        if y + h > sh - 8:
            y = max(8, int(anchor[1]) - h - self.options.offset_y)
        self.win.set_size_request(w, h)
        self.win.resize(w, h)
        self.win.move(x, y)
        try:
            saved = self._focus.get()
        except Exception:  # noqa: BLE001
            saved = None
        self.win.show_all()
        if saved is not None:
            try:
                self._focus.restore(saved)
            except Exception:  # noqa: BLE001
                pass
        self._hide_job = self._GLib.timeout_add(1200 if kind == "final" else 6000, self._auto_hide)

    def _pointer(self) -> tuple[int, int] | None:
        display = self._Gdk.Display.get_default()
        seat = display.get_default_seat() if display else None
        pointer = seat.get_pointer() if seat else None
        if pointer is None:
            return None
        _screen, px, py = pointer.get_position()
        return int(px), int(py)

    def _anchor(self) -> tuple[int, int] | None:
        """锚点优先级：光标(AT-SPI) → 窗口内的鼠标 → 聚焦窗口左下角 → 鼠标。

        鼠标落在聚焦窗口内时通常就在刚点击过的光标附近；移开后贴窗口左下角，
        避免候选条飘到无关位置。
        """
        anchor = self.tracker.get() if self.tracker else None
        if anchor is not None and anchor[0] > 0 and anchor[1] > 0:
            return anchor
        pointer = self._pointer()
        geo = self._focus.focused_geometry()
        if geo:
            gx, gy, gw, gh = geo
            if pointer and gx <= pointer[0] <= gx + gw and gy <= pointer[1] <= gy + gh:
                return pointer
            return int(gx + 28), int(gy + gh - 34)
        return pointer

    def _auto_hide(self) -> bool:
        self._hide()
        self._hide_job = None
        return False

    # ------------------------------------------------------------------ painting

    def _font(self) -> Any:
        return self._Pango.FontDescription(f"{self.options.font} 12")

    def _make_layout(self, text: str, max_text_w: int) -> Any:
        layout = self.win.create_pango_layout(text)
        layout.set_font_description(self._font())
        layout.set_wrap(self._Pango.WrapMode.CHAR)
        layout.set_ellipsize(self._Pango.EllipsizeMode.NONE)
        layout.set_width(max(64, max_text_w) * self._Pango.SCALE)
        return layout

    def _stable_bytes(self) -> int:
        if not (0 <= self._stable < len(self._text)):
            return -1
        return len(self._text[: self._stable].encode("utf-8"))

    def _measure(self) -> tuple[int, int]:
        usable = self.MAX_W - 2 * self.SHADOW - 2 * self.PAD_X - self.DOT_W
        text = self._text
        layout = self._make_layout(text, usable)
        while layout.get_line_count() > self.MAX_LINES and text:
            est = max(8, int(len(text) * self.MAX_LINES / layout.get_line_count()) + 4)
            text = text[est:]
            layout = self._make_layout(text, usable)
        if text != self._text:
            self._text = text
            self._stable = -1 if self._stable > len(self._text) else min(self._stable, len(self._text))
        _ink, logical = layout.get_pixel_extents()
        text_w = logical.width
        lines = max(1, layout.get_line_count())
        line_h = max(1, logical.height // lines)
        width = min(self.MAX_W, max(200, min(text_w, usable) + 2 * self.PAD_X + self.DOT_W + 2 * self.SHADOW))
        height = lines * line_h + 2 * self.PAD_Y + 2 * self.SHADOW
        return width, height

    def _round_rect(self, cr: Any, x: float, y: float, w: float, h: float, r: float) -> None:
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.arc(x + w - r, y + r, r, 1.5 * math.pi, 2.0 * math.pi)
        cr.arc(x + w - r, y + h - r, r, 0, 0.5 * math.pi)
        cr.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
        cr.close_path()

    def _draw(self, _win: Any, cr: Any) -> bool:
        w = _win.get_allocated_width()
        h = _win.get_allocated_height()
        if not self._rgba or self._cairo is None:
            cr.set_source_rgba(1, 1, 1, 1.0)
            cr.paint()
            self._draw_flat(cr, w, h)
            return False
        cr.set_operator(self._cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(self._cairo.OPERATOR_OVER)
        self._draw_shadow(cr, w, h)
        self._draw_card(cr, w, h)
        return False

    def _draw_shadow(self, cr: Any, w: int, h: int) -> None:
        layers = self.SHADOW
        for k in range(layers, 0, -1):
            grow = k
            alpha = 0.016 * (1.0 - k / (layers + 2)) ** 1.6
            cr.set_source_rgba(0.08, 0.12, 0.22, alpha)
            self._round_rect(cr, self.SHADOW - grow, self.SHADOW - grow + 1,
                             w - 2 * self.SHADOW + 2 * grow, h - 2 * self.SHADOW + 2 * grow,
                             self.RADIUS + grow)
            cr.fill()

    def _draw_card(self, cr: Any, w: int, h: int) -> None:
        cx = self.SHADOW
        cy = self.SHADOW
        cw = w - 2 * self.SHADOW
        ch = h - 2 * self.SHADOW
        cr.set_source_rgba(*self.CARD)
        self._round_rect(cr, cx, cy, cw, ch, self.RADIUS)
        cr.fill_preserve()
        cr.set_source_rgba(*self.BORDER)
        cr.set_line_width(1.0)
        cr.stroke()

        text_x = cx + self.PAD_X + (self.DOT_W if self._kind == "partial" else 0)
        text_y = cy + self.PAD_Y
        max_text_w = cw - 2 * self.PAD_X - (self.DOT_W if self._kind == "partial" else 0)
        layout = self._make_layout(self._text, max_text_w)
        attrs = self._Pango.AttrList()
        sb = self._stable_bytes()
        if sb >= 0:
            fg = self._Pango.attr_foreground_new(
                int(self.FG[0] * 65535), int(self.FG[1] * 65535), int(self.FG[2] * 65535)
            )
            fg.start_index, fg.end_index = 0, sb
            attrs.insert(fg)
            dim = self._Pango.attr_foreground_new(
                int(self.FG_DIM[0] * 65535), int(self.FG_DIM[1] * 65535), int(self.FG_DIM[2] * 65535)
            )
            dim.start_index, dim.end_index = sb, len(self._text.encode("utf-8"))
            attrs.insert(dim)
        layout.set_attributes(attrs)

        if self._kind == "partial":
            cr.set_source_rgba(*self.ACCENT)
            cr.arc(cx + self.PAD_X / 2 + 1, cy + self.PAD_Y + 9, 3.4, 0, 2 * math.pi)
            cr.fill()
        cr.move_to(text_x, text_y)
        self._PangoCairo.show_layout(cr, layout)

    def _draw_flat(self, cr: Any, w: int, h: int) -> None:
        cr.set_source_rgba(*self.FG)
        layout = self._make_layout(self._text, w - 2 * self.PAD_X)
        cr.move_to(self.PAD_X, self.PAD_Y)
        self._PangoCairo.show_layout(cr, layout)


def create_hud(options: Options, tracker: CaretTracker | None) -> Any:
    if sys.platform.startswith("linux"):
        hud = GtkHud(options, tracker)
        if hud.start():
            return hud
    hud = Hud(options, tracker)
    if hud.start():
        return hud
    return None


# --------------------------------------------------------------------------- HTTP


class LocalHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], agent: "Agent") -> None:
        self.agent = agent
        super().__init__(address, LocalHandler)


class LocalHandler(BaseHTTPRequestHandler):
    server: LocalHttpServer

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.debug("local http: " + fmt, *args)

    def _reply(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = self.server.agent.options.token
        if not token or token == "change-me":
            return True
        supplied = query.get("token", [""])[0] or self.headers.get("X-Voice-Token", "")
        return supplied == token

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._reply(
                200,
                {
                    "ok": True,
                    "host": self.server.agent.options.host,
                    "active": self.server.agent.active,
                    "dry_run": self.server.agent.options.dry_run,
                },
            )
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._reply(403, {"ok": False, "error": "bad token"})
            return
        if parsed.path == "/active":
            state = (query.get("state", ["enter"])[0] or "enter").lower()
            self.server.agent.set_active(state == "enter")
            self._reply(200, {"ok": True, "active": self.server.agent.active})
        elif parsed.path == "/insert":
            text = query.get("text", [""])[0]
            try:
                backend = self.server.agent.inject_local(text)
                self._reply(200, {"ok": True, "backend": backend})
            except Exception as exc:  # noqa: BLE001
                self._reply(500, {"ok": False, "error": str(exc)})
        else:
            self._reply(404, {"ok": False, "error": "not found"})


# --------------------------------------------------------------------------- agent


class Agent:
    def __init__(self, options: Options, hud: Hud | None) -> None:
        self.options = options
        self.hud = hud
        self.injector = Injector(options)
        self.dedupe = Dedupe()
        self.active = False
        self.connected = False
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.stop_event = threading.Event()

    def set_active(self, active: bool) -> None:
        with self.state_lock:
            if self.active == active:
                return
            self.active = active
        LOG.info("active=%s", active)
        self.send({"type": "active", "host": self.options.host, "state": "enter" if active else "leave", "ts": time.time()})

    def send(self, message: dict[str, Any]) -> bool:
        sock = self.sock
        if sock is None or not self.connected:
            return False
        try:
            with self.send_lock:
                sock.sendall(encode_message(message))
            return True
        except OSError as exc:
            LOG.warning("send failed: %s", exc)
            return False

    def inject_local(self, text: str) -> str:
        return self.injector.inject(text)

    def run_forever(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                LOG.warning("connection problem: %s", exc)
            finally:
                self.connected = False
                self.sock = None
            if self.stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _session(self) -> None:
        LOG.info("connecting to %s:%s", self.options.server, self.options.port)
        sock = socket.create_connection((self.options.server, self.options.port), timeout=8)
        sock.settimeout(None)
        self.sock = sock
        self.buffer.clear()
        with self.send_lock:
            sock.sendall(
                encode_message(
                    {
                        "type": "hello",
                        "version": PROTOCOL_VERSION,
                        "role": "agent",
                        "host": self.options.host,
                        "token": self.options.token,
                    }
                )
            )
        ack = read_line(sock, self.buffer)
        if not ack.get("ok"):
            raise RuntimeError(f"auth failed: {ack.get('error')}")
        self.connected = True
        LOG.info("connected as %s", self.options.host)
        if self.active:
            self.send({"type": "active", "host": self.options.host, "state": "enter", "ts": time.time()})
        while not self.stop_event.is_set():
            message = read_line(sock, self.buffer)
            self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "insert_text":
            request_id = str(message.get("request_id", ""))
            text = str(message.get("text", ""))
            if self.dedupe.seen(request_id):
                self.send({"type": "ack", "request_id": request_id, "ok": True, "error": None})
                return
            try:
                backend = self.injector.inject(text)
                if self.hud is not None:
                    # 先在候选框里显示最终文本（1.2s 后自动消失），让用户看到"更到最新"的结果
                    self.hud.submit("final", text)
                LOG.info("inserted %d chars via %s", len(text), backend)
                self.send({"type": "ack", "request_id": request_id, "ok": True, "error": None})
            except Exception as exc:  # noqa: BLE001
                LOG.error("inject failed: %s", exc)
                self.send({"type": "ack", "request_id": request_id, "ok": False, "error": str(exc)})
        elif kind == "partial":
            if self.hud is not None:
                self.hud.submit("partial", str(message.get("text", "")), int(message.get("stable_len", -1)))
        elif kind == "final":
            if self.hud is not None:
                self.hud.submit("final", str(message.get("text", "")))
        elif kind == "ping":
            self.send({"type": "pong", "ts": time.time()})
        elif kind == "hello_ack":
            pass
        else:
            LOG.debug("ignored message type: %s", kind)

    def heartbeat(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(5)
            if self.active and self.connected:
                self.send({"type": "heartbeat", "host": self.options.host, "active": True, "ts": time.time()})

    def stop(self) -> None:
        self.stop_event.set()
        sock = self.sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="voice-tunnel agent")
    parser.add_argument("--config", default="config.ini")
    parser.add_argument("--server")
    parser.add_argument("--port", type=int)
    parser.add_argument("--token")
    parser.add_argument("--host")
    parser.add_argument("--local-port", type=int)
    hud_group = parser.add_mutually_exclusive_group()
    hud_group.add_argument("--hud", dest="hud", action="store_true")
    hud_group.add_argument("--no-hud", dest="hud", action="store_false")
    parser.set_defaults(hud=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-clipboard-fallback", action="store_true")
    parser.add_argument("--inject-mode", choices=["auto", "xdotool", "clipboard"])
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _acquire_instance_lock() -> Any:
    """单实例锁：文件锁（Windows msvcrt / POSIX flock），进程退出或死亡自动释放。

    早期用固定端口 bind 做锁，在 Windows 上会被 WSL/Hyper-V 动态端口范围、
    僵尸进程等环境因素干扰，改为文件锁后与端口无关。
    """
    lock_path = os.environ.get(
        "VOICE_TUNNEL_AGENT_LOCK", os.path.join(tempfile.gettempdir(), "voice-tunnel-agent.lock")
    )
    handle = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main() -> int:
    # 单实例锁：占住文件锁，重复启动的实例直接退出（任务计划/脚本清理不可靠）
    os.environ.setdefault("NO_AT_BRIDGE", "1")
    lock_handle = _acquire_instance_lock()
    if lock_handle is None:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger("voice_tunnel.agent").error(
            "another voice-tunnel agent is already running (instance lock busy); exiting"
        )
        return 1
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    options = load_options(args)
    if options.token == "change-me":
        LOG.warning("using default token; change it in config.ini")
    if not options.host:
        options.host = platform.node()

    # 先建 Agent 并启动网络/看门狗，HUD 初始化放在最后——个别机器上 Tk/GTK
    # 会卡死（锁屏/会话状态），不能让它阻塞连接与注入
    agent = Agent(options, None)
    httpd: LocalHttpServer | None = None
    try:
        httpd = LocalHttpServer(("127.0.0.1", options.local_port), agent)
    except OSError as exc:
        LOG.warning("local event endpoint unavailable on %s: %s", options.local_port, exc)

    threads: list[threading.Thread] = []
    if httpd is not None:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="local-http")
        thread.start()
        threads.append(thread)
    heartbeat = threading.Thread(target=agent.heartbeat, daemon=True, name="heartbeat")
    heartbeat.start()
    threads.append(heartbeat)

    network = threading.Thread(target=agent.run_forever, daemon=True, name="network")
    network.start()
    threads.append(network)

    # 启动看门狗：25s 内没有完成首次连接就整个进程退出（释放单实例锁），
    # 避免个别机器上 Tk/GTK 初始化卡死造成"僵尸实例占锁"
    def _startup_watchdog() -> None:
        deadline = time.time() + 25
        while time.time() < deadline and not agent.stop_event.is_set():
            if agent.connected:
                return
            time.sleep(1)
        if not agent.connected:
            LOG.error("startup watchdog: no connection in 25s, exiting to free the lock")
            os._exit(1)

    watchdog = threading.Thread(target=_startup_watchdog, daemon=True, name="startup-watchdog")
    watchdog.start()

    hud: Any = None
    tracker: CaretTracker | None = None
    if options.hud:
        tracker = CaretTracker()
        tracker.start()
        hud = create_hud(options, tracker)
        if hud is None:
            tracker.stop()
            tracker = None
        else:
            agent.hud = hud

    LOG.info("agent ready: host=%s server=%s:%s hud=%s dry_run=%s", options.host, options.server, options.port, hud is not None, options.dry_run)
    try:
        if hud is not None:
            hud.run()
        else:
            while not agent.stop_event.is_set():
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
        if httpd is not None:
            httpd.shutdown()
        if tracker is not None:
            tracker.stop()
        if hud is not None:
            hud.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
