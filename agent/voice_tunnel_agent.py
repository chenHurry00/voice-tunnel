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
    CLIPBOARD_RESTORE_DELAY = 0.8  # 目标程序异步读取剪贴板，过早还原会粘贴出旧内容

    def __init__(self, options: Options) -> None:
        self.options = options
        # 剪贴板注入期间持有，后台还原完成后释放；保证连续两句不会互相覆盖
        self._clipboard_busy = threading.Lock()

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

        class INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        raw = text.encode("utf-16-le")
        units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
        events = (INPUT * (2 * len(units)))()
        for index, unit in enumerate(units):
            for offset, flags in enumerate((KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)):
                events[2 * index + offset] = INPUT(
                    type=INPUT_KEYBOARD, union=INPUTUNION(ki=KEYBDINPUT(0, unit, flags, 0, 0))
                )
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        # 一次提交整段：原子、无逐字延迟（被 UIPI 拦截时返回 0）
        sent = user32.SendInput(len(events), events, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise RuntimeError(f"SendInput sent {sent}/{len(events)} (error={ctypes.get_last_error()}; elevated window?)")

    # -- Linux X11 -------------------------------------------------------

    def _inject_linux(self, text: str) -> str:
        mode = self.options.inject_mode
        non_ascii = any(ord(ch) > 127 for ch in text)
        if mode == "clipboard" or (mode == "auto" and self.options.allow_clipboard_fallback and non_ascii):
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
            wid = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2).stdout.strip()
            if wid and shutil.which("xprop"):
                prop = subprocess.run(["xprop", "-id", wid, "WM_CLASS"], capture_output=True, text=True, timeout=2).stdout
                parts = re.findall(r'"([^"]*)"', prop)
                if parts:
                    return parts[-1].lower()
        except (OSError, subprocess.SubprocessError):
            pass
        return ""

    def _paste_chord(self) -> str:
        window_class = self._active_window_class()
        terminals = [item.strip().lower() for item in self.options.terminal_classes.split(",") if item.strip()]
        return "ctrl+shift+v" if any(name in window_class for name in terminals) else "ctrl+v"

    def _inject_linux_clipboard(self, text: str) -> str:
        if not shutil.which("xclip") or not shutil.which("xdotool"):
            raise RuntimeError("clipboard fallback needs xclip and xdotool")
        self._clipboard_busy.acquire()
        old = None
        try:
            saved = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=3)
            if saved.returncode == 0:
                old = saved.stdout
            subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True, timeout=5)
            time.sleep(0.05)
            chord = self._paste_chord()
            subprocess.run(["xdotool", "key", "--clearmodifiers", chord], check=True, timeout=5)
        except Exception:
            self._clipboard_busy.release()
            raise
        # 还原放后台：粘贴已发出，ACK 不必再等 0.8s
        threading.Thread(target=self._restore_clipboard, args=(old,), daemon=True, name="clip-restore").start()
        return f"clipboard:{chord}"

    def _restore_clipboard(self, old: str | None) -> None:
        try:
            time.sleep(self.CLIPBOARD_RESTORE_DELAY)
            if old:
                subprocess.run(["xclip", "-selection", "clipboard"], input=old, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            LOG.warning("failed to restore previous clipboard text")
        finally:
            self._clipboard_busy.release()


# --------------------------------------------------------------------------- caret / geometry (Windows)

class Win32:
    """Windows 几何查询：输入光标、前台窗口、鼠标、显示器工作区（物理像素，需 DPI 感知）。"""

    def __init__(self) -> None:
        from ctypes import wintypes

        self.wt = wintypes
        self.user32 = ctypes.windll.user32
        self.oleacc = ctypes.windll.oleacc
        ctypes.windll.ole32.CoInitialize(None)

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

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

        class GUID(ctypes.Structure):
            _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

        class _VariantData(ctypes.Union):
            _fields_ = [("lVal", ctypes.c_long), ("record", ctypes.c_void_p * 2)]

        class VARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort), ("data", _VariantData)]

        self.RECT, self.GUITHREADINFO, self.MONITORINFO, self.VARIANT = RECT, GUITHREADINFO, MONITORINFO, VARIANT
        self.iid_accessible = GUID(0x618736E0, 0x3C3D, 0x11CF, (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))
        HWND = wintypes.HWND
        self.user32.GetForegroundWindow.restype = HWND
        self.user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.c_void_p]
        self.user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
        self.user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
        self.user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
        self.user32.MonitorFromPoint.restype = wintypes.HANDLE
        self.user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        self.oleacc.AccessibleObjectFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.oleacc.AccessibleObjectFromWindow.restype = ctypes.c_long

    def caret(self) -> tuple[int, int, int, int] | None:
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd:
            return None
        info = self.GUITHREADINFO(cbSize=ctypes.sizeof(self.GUITHREADINFO))
        if not self.user32.GetGUIThreadInfo(self.user32.GetWindowThreadProcessId(hwnd, None), ctypes.byref(info)):
            return None
        if info.hwndCaret:  # Win32 原生控件（记事本、多数传统程序）
            rc = info.rcCaret
            point = self.wt.POINT(rc.left, rc.top)
            if self.user32.ClientToScreen(info.hwndCaret, ctypes.byref(point)):
                return int(point.x), int(point.y), max(1, rc.right - rc.left), max(1, rc.bottom - rc.top)
        return self._msaa_caret(info.hwndFocus or hwnd)

    def _msaa_caret(self, hwnd: Any) -> tuple[int, int, int, int] | None:
        """MSAA OBJID_CARET：Chrome / Electron / VS Code 等自绘控件会暴露这个对象。"""
        OBJID_CARET = 0xFFFFFFF8
        acc = ctypes.c_void_p()
        if self.oleacc.AccessibleObjectFromWindow(hwnd, OBJID_CARET, ctypes.byref(self.iid_accessible), ctypes.byref(acc)) != 0 or not acc.value:
            return None
        vtable = ctypes.cast(acc.value, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        try:
            long_p = ctypes.POINTER(ctypes.c_long)
            acc_location = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, long_p, long_p, long_p, long_p, self.VARIANT)(vtable[22])
            x, y, w, h = (ctypes.c_long() for _ in range(4))
            child_self = self.VARIANT(vt=3)  # VT_I4, CHILDID_SELF=0
            if acc_location(acc, ctypes.byref(x), ctypes.byref(y), ctypes.byref(w), ctypes.byref(h), child_self) != 0:
                return None
            if (x.value, y.value) == (0, 0) or h.value <= 0:
                return None
            return int(x.value), int(y.value), max(1, int(w.value)), int(h.value)
        finally:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(acc)  # Release

    def foreground_rect(self) -> tuple[int, int, int, int] | None:
        hwnd = self.user32.GetForegroundWindow()
        rect = self.RECT()
        if not hwnd or not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top)

    def pointer(self) -> tuple[int, int] | None:
        point = self.wt.POINT()
        return (int(point.x), int(point.y)) if self.user32.GetCursorPos(ctypes.byref(point)) else None

    def work_area(self, x: int, y: int) -> tuple[int, int, int, int]:
        monitor = self.user32.MonitorFromPoint(self.wt.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
        info = self.MONITORINFO(cbSize=ctypes.sizeof(self.MONITORINFO))
        if monitor and self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rc = info.rcWork
            return int(rc.left), int(rc.top), int(rc.right - rc.left), int(rc.bottom - rc.top)
        return 0, 0, self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1)


# --------------------------------------------------------------------------- HUD


def _common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


class HudModel:
    """浮窗状态（与绘制无关）：聆听 → 识别中 → 收尾 → 提交即消失；错误短暂提示。"""

    TIMEOUTS = {"listening": 8.0, "finishing": 6.0, "error": 2.5}  # 无新事件时自动收起

    def __init__(self) -> None:
        self.mode = "hidden"
        self.text = ""
        self.stable = 0
        self.shown = 0.0  # 已显现的字符数（逐字显现动画）
        self.level = 0.0
        self.level_target = 0.0
        self.phase = 0.0
        self.deadline = 0.0
        self.new_session = False

    @property
    def visible(self) -> bool:
        return self.mode != "hidden"

    EVENTS = {"listen", "level", "partial", "finish", "commit", "error", "hide"}

    def apply(self, event: str, text: str = "", stable: int = -1, level: float | None = None) -> None:
        if event not in self.EVENTS:
            return
        if event in {"hide", "commit"}:
            self.mode = "hidden"
            return
        if event in {"level", "finish"} and not self.visible:
            return  # 迟到的事件不应重新弹出浮窗
        if event == "listen" or (event in {"partial", "error"} and not self.visible):
            self.text, self.stable, self.shown, self.level = "", 0, 0.0, 0.0
            self.new_session = True
        if event == "partial":
            # 被改写的部分立即替换，新增部分逐字显现
            self.shown = min(self.shown, _common_prefix_len(self.text, text))
            self.text = text
            self.stable = len(text) if stable < 0 else min(stable, len(text))
            self.mode = "listening"
        elif event == "listen":
            self.mode = "listening"
        elif event == "finish":
            self.mode = "finishing"
            self.stable = len(self.text)
        elif event == "error":
            self.text, self.stable, self.shown = text, len(text), float(len(text))
            self.mode = "error"
        if level is not None:
            self.level_target = max(0.0, min(1.0, float(level)))
        self.deadline = time.monotonic() + self.TIMEOUTS[self.mode]

    def tick(self, dt: float) -> bool:
        """推进动画，返回是否需要重绘。"""
        if not self.visible:
            return False
        if time.monotonic() > self.deadline:
            self.mode = "hidden"
            return True
        changed = False
        total = len(self.text)
        if self.shown < total:
            self.shown = min(total, self.shown + max(1.0, (total - self.shown) * 0.35))
            changed = True
        target = self.level_target if self.mode == "listening" else 0.0
        level = self.level + (target - self.level) * min(1.0, dt * 14)
        changed = changed or abs(level - self.level) > 0.003
        self.level = level
        self.phase += dt
        return changed or self.mode == "finishing" or (self.mode == "listening" and self.level > 0.02)

    def display(self) -> tuple[str, int]:
        shown = int(self.shown)
        return self.text[:shown], min(self.stable, shown)


class HudStyle:
    """两套渲染器共用的尺寸（逻辑像素）与配色。"""

    PAD_X = 12
    PAD_Y = 7
    BARS_W = 13      # 3 根音量条
    BARS_GAP = 8     # 音量条与文字的间距
    RADIUS = 9
    MAX_W = 520
    MAX_LINES = 3
    GAP = 6          # 与光标的距离
    TAIL_CHARS = 240  # 最多只排版末尾这么多字（只显示 3 行）
    CARD = "#ffffff"
    BORDER = "#dcdfe4"
    TEXT = "#1f2329"
    TAIL = "#8f959e"
    BAR = "#4e5969"
    BAR_IDLE = "#b4b9c1"
    ERROR = "#d83931"

    @staticmethod
    def rgb(color: str, alpha: float = 1.0) -> tuple[float, float, float, float]:
        value = int(color[1:], 16)
        return ((value >> 16) / 255, ((value >> 8) & 0xFF) / 255, (value & 0xFF) / 255, alpha)

    @staticmethod
    def bar_heights(model: HudModel, line_h: float) -> list[float]:
        low, high = 3.0, line_h * 0.72
        heights = []
        for i, weight in enumerate((0.62, 1.0, 0.78)):
            wobble = 0.82 + 0.18 * math.sin(model.phase * 9.0 + i * 1.9)
            heights.append(low + (high - low) * min(1.0, model.level * weight * wobble))
        return heights

    @staticmethod
    def dot_alpha(model: HudModel, index: int) -> float:
        """收尾态：三个点依次明暗，表示正在整段校正。"""
        return 0.35 + 0.65 * max(0.0, math.sin(model.phase * 6.0 - index * 0.9)) ** 2


def place_card(
    anchor: tuple[int, int, int, int], size: tuple[int, int], text_inset: int, area: tuple[int, int, int, int]
) -> tuple[int, int]:
    """候选框放在光标正下方、文字起点对齐光标（输入法习惯）；下方放不下就放上方。"""
    ax, ay, _aw, ah = anchor
    w, h = size
    left, top, width, height = area
    x = ax - text_inset
    y = ay + ah + HudStyle.GAP
    if y + h > top + height:
        y = ay - h - HudStyle.GAP
    x = max(left, min(x, left + width - w))
    y = max(top, min(y, top + height - h))
    return int(x), int(y)


class HudBase:
    """事件 → 状态 → 定位/尺寸；子类只负责具体窗口与绘制。"""

    def __init__(self, options: Options) -> None:
        self.options = options
        self.model = HudModel()
        self.scale = 1.0
        self._anchor_rect: tuple[int, int, int, int] | None = None
        self._card_w = 0

    # 子类实现 ---------------------------------------------------------------
    def _caret(self) -> tuple[int, int, int, int] | None:
        raise NotImplementedError

    def _pointer(self) -> tuple[int, int] | None:
        raise NotImplementedError

    def _focused_rect(self) -> tuple[int, int, int, int] | None:
        raise NotImplementedError

    def _work_area(self, x: int, y: int) -> tuple[int, int, int, int]:
        raise NotImplementedError

    # 共用逻辑 ---------------------------------------------------------------
    def _px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _anchor(self) -> tuple[int, int, int, int] | None:
        """锚点：输入光标 → 聚焦窗口内的鼠标 → 聚焦窗口左下 → 鼠标。"""
        caret = self._caret()
        if caret:
            self._anchor_kind = getattr(self, "_caret_kind", "") or "caret"
            return caret
        pointer = self._pointer()
        window = self._focused_rect()
        ox, oy = self._px(self.options.offset_x), self._px(self.options.offset_y)
        self._anchor_kind = "pointer"
        if pointer and window:
            wx, wy, ww, wh = window
            if wx <= pointer[0] <= wx + ww and wy <= pointer[1] <= wy + wh:
                return pointer[0] + ox, pointer[1], 0, oy
        if window:
            self._anchor_kind = "window"
            return window[0] + self._px(24), window[1] + window[3] - self._px(64), 0, 0
        if pointer:
            return pointer[0] + ox, pointer[1], 0, oy
        return None

    def _update_anchor(self, events: bool) -> None:
        if not events and self._anchor_rect is not None:
            return  # 纯动画帧不查询光标（Windows 上 MSAA 是跨进程调用）
        if self.model.new_session:
            self.model.new_session = False
            self._card_w = 0
            self._anchor_rect = self._anchor()
            LOG.info("hud anchor %s via %s", self._anchor_rect, getattr(self, "_anchor_kind", "?"))
            return
        caret = self._caret()  # 只跟随光标；回退锚点（鼠标）在一句话内保持不动，避免浮窗乱跳
        if caret and caret != self._anchor_rect:
            self._anchor_rect = caret

    def _card_width(self, text_w: int) -> int:
        st = HudStyle
        width = self._px(st.PAD_X * 2 + st.BARS_W) + (self._px(st.BARS_GAP) + text_w if text_w else 0)
        # 一句话内只增不减，避免宽度来回抖动
        self._card_w = min(self._px(st.MAX_W), max(self._card_w, width))
        return self._card_w

    def _wrap_width(self) -> int:
        st = HudStyle
        return self._px(st.MAX_W - st.PAD_X * 2 - st.BARS_W - st.BARS_GAP)

    def _text_inset(self) -> int:
        st = HudStyle
        return self._px(st.PAD_X + st.BARS_W + st.BARS_GAP)


class GtkHud(HudBase):
    """Linux：GTK3 override-redirect 弹层（不经窗口管理器，永不抢焦点）+ cairo 圆角阴影；位置跟随鼠标。"""

    SHADOW = 10

    def __init__(self, options: Options) -> None:
        super().__init__(options)
        self.win: Any = None
        self._timer: Any = None
        self._last_tick = 0.0
        self._layout: Any = None
        self._stable_bytes = 0
        self._line_h = 20
        self._rgba = False
        self._geometry: tuple[int, int, int, int] | None = None

    def start(self) -> bool:
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            gi.require_version("PangoCairo", "1.0")
            from gi.repository import GLib, Gdk, Gtk, Pango, PangoCairo

            ok = Gtk.init_check(None)
            if not (ok[0] if isinstance(ok, tuple) else ok):
                raise RuntimeError("cannot open display")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("GTK HUD unavailable: %s", exc)
            return False
        self.Gtk, self.Gdk, self.GLib, self.Pango, self.PangoCairo = Gtk, Gdk, GLib, Pango, PangoCairo
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_app_paintable(True)
        win.set_accept_focus(False)
        win.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
        visual = win.get_screen().get_rgba_visual()
        if visual is not None and win.get_screen().is_composited():
            win.set_visual(visual)
            self._rgba = True
        win.connect("draw", self._draw)
        self.win = win
        self._font = Pango.FontDescription(f"{self.options.font} 11")
        probe = win.create_pango_layout("中Ag")
        probe.set_font_description(self._font)
        self._line_h = probe.get_pixel_size()[1]
        LOG.info("HUD backend: gtk3 (rgba=%s)", self._rgba)
        return True

    def run(self) -> None:
        self.Gtk.main()

    def stop(self) -> None:
        self.GLib.idle_add(self.Gtk.main_quit)

    def submit(self, event: str, text: str = "", stable: int = -1, level: float | None = None) -> None:
        self.GLib.idle_add(self._on_event, event, text, stable, level)

    # -- 平台查询 -----------------------------------------------------------
    def _caret(self) -> tuple[int, int, int, int] | None:
        return None  # Ubuntu 统一跟随鼠标（多数程序拿不到可靠的输入光标）

    def _pointer(self) -> tuple[int, int] | None:
        seat = self.Gdk.Display.get_default().get_default_seat()
        pointer = seat.get_pointer() if seat else None
        if pointer is None:
            return None
        _screen, x, y = pointer.get_position()
        return int(x), int(y)

    def _focused_rect(self) -> tuple[int, int, int, int] | None:
        return None

    def _work_area(self, x: int, y: int) -> tuple[int, int, int, int]:
        display = self.Gdk.Display.get_default()
        monitor = display.get_monitor_at_point(x, y) or display.get_primary_monitor()
        rect = monitor.get_workarea()
        return rect.x, rect.y, rect.width, rect.height

    # -- 事件与动画 ---------------------------------------------------------
    def _on_event(self, event: str, text: str, stable: int, level: float | None) -> bool:
        self.model.apply(event, text, stable, level)
        self._sync(events=True)
        if self.model.visible and self._timer is None:
            self._last_tick = time.monotonic()
            self._timer = self.GLib.timeout_add(16, self._frame)
        return False

    def _frame(self) -> bool:
        now = time.monotonic()
        changed = self.model.tick(now - self._last_tick)
        self._last_tick = now
        if changed or not self.model.visible:
            self._sync(events=False)
        if not self.model.visible:
            self._timer = None
            return False
        return True

    def _make_layout(self, text: str) -> Any:
        layout = self.win.create_pango_layout(text)
        layout.set_font_description(self._font)
        layout.set_wrap(self.Pango.WrapMode.WORD_CHAR)
        layout.set_width(self._wrap_width() * self.Pango.SCALE)
        return layout

    def _sync(self, events: bool) -> None:
        if not self.model.visible:
            self.win.hide()
            return
        st = HudStyle
        text, stable = self.model.display()
        cut = max(0, len(text) - st.TAIL_CHARS)
        text, stable = text[cut:], max(0, stable - cut)
        layout = self._make_layout(text)
        while layout.get_line_count() > st.MAX_LINES:
            # 超过 3 行：只保留末尾 3 行，前面用省略号
            start = layout.get_line(layout.get_line_count() - st.MAX_LINES).start_index
            drop = len(text.encode("utf-8")[:start].decode("utf-8", "ignore"))
            text, stable = "…" + text[drop + 1 :], max(1, stable - drop)
            layout = self._make_layout(text)
        attrs = self.Pango.AttrList()
        split = len(text[:stable].encode("utf-8"))
        for color, start, end in ((st.TEXT, 0, split), (st.TAIL, split, len(text.encode("utf-8")))):
            if self.model.mode == "error":
                color = st.ERROR
            r, g, b, _a = st.rgb(color)
            attr = self.Pango.attr_foreground_new(int(r * 65535), int(g * 65535), int(b * 65535))
            attr.start_index, attr.end_index = start, end
            attrs.insert(attr)
        layout.set_attributes(attrs)
        self._layout = layout
        text_w, text_h = layout.get_pixel_size() if text else (0, 0)
        card_w = self._card_width(text_w)
        card_h = st.PAD_Y * 2 + max(self._line_h, text_h)
        self._update_anchor(events)
        if self._anchor_rect is None:
            return
        area = self._work_area(self._anchor_rect[0], self._anchor_rect[1])
        x, y = place_card(self._anchor_rect, (card_w, card_h), self._text_inset(), area)
        s = self.SHADOW if self._rgba else 0
        geometry = (x - s, y - s, card_w + 2 * s, card_h + 2 * s)
        if geometry != self._geometry:
            self._geometry = geometry
            self.win.resize(geometry[2], geometry[3])
            self.win.move(geometry[0], geometry[1])
        self.win.queue_draw()
        if not self.win.get_visible():
            self.win.show()

    # -- 绘制 -------------------------------------------------------------
    def _round_rect(self, cr: Any, x: float, y: float, w: float, h: float, r: float) -> None:
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()

    def _draw(self, widget: Any, cr: Any) -> bool:
        import cairo

        st = HudStyle
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        s = self.SHADOW if self._rgba else 0
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cw, ch = w - 2 * s, h - 2 * s
        if s:
            for k in range(s, 0, -1):  # 柔和投影：向下偏 2px，逐层扩散
                cr.set_source_rgba(0.06, 0.09, 0.16, 0.011 * (1 - k / (s + 1)) ** 1.5 * 3)
                self._round_rect(cr, s - k, s - k + 2, cw + 2 * k, ch + 2 * k, st.RADIUS + k)
                cr.fill()
        self._round_rect(cr, s + 0.5, s + 0.5, cw - 1, ch - 1, st.RADIUS)
        cr.set_source_rgba(*st.rgb(st.CARD))
        cr.fill_preserve()
        cr.set_source_rgba(*st.rgb(st.BORDER))
        cr.set_line_width(1.0)
        cr.stroke()
        # 指示：聆听=音量条，收尾=三点呼吸，错误=红点
        bx = s + st.PAD_X + 1.5
        cy = s + st.PAD_Y + self._line_h / 2
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(3.0)
        if self.model.mode == "error":
            cr.set_source_rgba(*st.rgb(st.ERROR))
            cr.arc(bx + 5, cy, 3.5, 0, 2 * math.pi)
            cr.fill()
        else:
            heights = st.bar_heights(self.model, self._line_h)
            for i in range(3):
                x = bx + i * 5
                if self.model.mode == "finishing":
                    cr.set_source_rgba(*st.rgb(st.BAR, st.dot_alpha(self.model, i)))
                    half = 1.5
                else:
                    cr.set_source_rgba(*st.rgb(st.BAR))
                    half = max(1.5, heights[i] / 2)
                cr.move_to(x, cy - half + 1.5)
                cr.line_to(x, cy + half - 1.5 + 0.01)
                cr.stroke()
        if self._layout is not None and self.model.text:
            cr.move_to(s + self._text_inset(), s + st.PAD_Y)
            self.PangoCairo.show_layout(cr, self._layout)
        return False


class TkHud(HudBase):
    """Windows（及无 GTK 的 Linux）：Tk 画布 + 系统投影 + 不激活显示。"""

    def __init__(self, options: Options) -> None:
        super().__init__(options)
        self.queue: queue.Queue[tuple[str, str, int, float | None]] = queue.Queue()
        self.root: Any = None
        self._win32: Win32 | None = None
        self._hwnd = 0
        self._shown = False
        self._geometry: tuple[int, int, int, int] | None = None
        self._char_w: dict[str, int] = {}
        self._last_tick = time.monotonic()

    def start(self) -> bool:
        try:
            import tkinter as tk
            import tkinter.font as tkfont

            root = tk.Tk()
            root.withdraw()
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            self.scale = root.winfo_fpixels("1i") / 96.0
            self.canvas = tk.Canvas(root, highlightthickness=0, borderwidth=0, background=HudStyle.CARD)
            self.canvas.pack(fill="both", expand=True)
            self.font = tkfont.Font(root=root, family=self.options.font, size=11)
            self._line_h = self.font.metrics("linespace")
            self.root = root
            if os.name == "nt":
                self._win32 = Win32()
                root.geometry("1x1+-32000+-32000")
                root.deiconify()
                root.update_idletasks()
                self._hwnd = int(root.wm_frame(), 16)
                self._win_style()
                self._win_show(False)
        except Exception as exc:  # noqa: BLE001 - HUD is optional
            LOG.warning("HUD disabled: %s", exc)
            self.root = None
            return False
        root.after(30, self._poll)
        LOG.info("HUD backend: tk (scale=%.2f)", self.scale)
        return True

    def run(self) -> None:
        self.root.mainloop()

    def stop(self) -> None:
        try:
            self.root.after(0, self.root.quit)
        except Exception:  # noqa: BLE001
            pass

    def submit(self, event: str, text: str = "", stable: int = -1, level: float | None = None) -> None:
        self.queue.put((event, text, stable, level))

    # -- 平台查询 -----------------------------------------------------------
    def _caret(self) -> tuple[int, int, int, int] | None:
        if self._win32 is None:
            return None
        try:
            return self._win32.caret()
        except Exception:  # noqa: BLE001
            return None

    def _pointer(self) -> tuple[int, int] | None:
        if self._win32 is not None:
            return self._win32.pointer()
        return self.root.winfo_pointerx(), self.root.winfo_pointery()

    def _focused_rect(self) -> tuple[int, int, int, int] | None:
        if self._win32 is not None:
            return self._win32.foreground_rect()
        return None

    def _work_area(self, x: int, y: int) -> tuple[int, int, int, int]:
        if self._win32 is not None:
            return self._win32.work_area(x, y)
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    # -- Windows 窗口样式 ----------------------------------------------------
    def _win_style(self) -> None:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE, GCL_STYLE = -20, -26
        WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x8, 0x80, 0x08000000
        CS_DROPSHADOW = 0x00020000
        style = user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, style | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        set_class = getattr(user32, "SetClassLongPtrW", user32.SetClassLongW)
        get_class = getattr(user32, "GetClassLongPtrW", user32.GetClassLongW)
        set_class.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        get_class.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_class.restype = ctypes.c_ssize_t
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        set_class(self._hwnd, GCL_STYLE, get_class(self._hwnd, GCL_STYLE) | CS_DROPSHADOW)
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]

    def _win_show(self, show: bool, geometry: tuple[int, int, int, int] | None = None) -> None:
        user32 = ctypes.windll.user32
        if not show:
            user32.ShowWindow(self._hwnd, 0)  # SW_HIDE
            return
        x, y, w, h = geometry or (0, 0, 1, 1)
        SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x10, 0x40
        user32.SetWindowPos(self._hwnd, ctypes.c_void_p(-1), x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)  # HWND_TOPMOST，不激活

    # -- 事件与动画 ---------------------------------------------------------
    def _poll(self) -> None:
        try:
            dirty = False
            while True:
                try:
                    event, text, stable, level = self.queue.get_nowait()
                except queue.Empty:
                    break
                self.model.apply(event, text, stable, level)
                dirty = True
            events = dirty
            now = time.monotonic()
            dirty = self.model.tick(now - self._last_tick) or dirty
            self._last_tick = now
            if dirty or (self._shown and not self.model.visible):
                self._sync(events)
        except Exception:  # noqa: BLE001 - 单帧失败不能让轮询停掉（停掉浮窗就再也不更新）
            LOG.exception("HUD render failed")
        finally:
            self.root.after(16 if self.model.visible else 40, self._poll)

    def _measure(self, text: str) -> int:
        total = 0
        for ch in text:
            width = self._char_w.get(ch)
            if width is None:
                width = self._char_w[ch] = self.font.measure(ch)
            total += width
        return total

    def _wrap(self, text: str) -> list[tuple[int, int]]:
        """按字符折行，返回每行 (起, 止) 下标。"""
        limit = self._wrap_width()
        lines, start, width = [], 0, 0
        for index, ch in enumerate(text):
            cw = self._char_w.get(ch) or self._measure(ch)
            if width + cw > limit and index > start:
                lines.append((start, index))
                start, width = index, 0
            width += cw
        lines.append((start, len(text)))
        return lines

    def _sync(self, events: bool) -> None:
        if not self.model.visible:
            self._hide()
            return
        st = HudStyle
        text, stable = self.model.display()
        cut = max(0, len(text) - st.TAIL_CHARS)
        text, stable = text[cut:], max(0, stable - cut)
        lines = self._wrap(text) if text else []
        if len(lines) > st.MAX_LINES:
            drop = lines[-st.MAX_LINES][0]
            text, stable = "…" + text[drop + 1 :], max(1, stable - drop)
            lines = self._wrap(text)[-st.MAX_LINES :]
        text_w = max((self._measure(text[a:b]) for a, b in lines), default=0)
        card_w = self._card_width(text_w)
        card_h = self._px(st.PAD_Y * 2) + self._line_h * max(1, len(lines))
        self._update_anchor(events)
        if self._anchor_rect is None:
            return
        area = self._work_area(self._anchor_rect[0], self._anchor_rect[1])
        x, y = place_card(self._anchor_rect, (card_w, card_h), self._text_inset(), area)
        self._paint(text, stable, lines, card_w, card_h)
        geometry = (x, y, card_w, card_h)
        if geometry != self._geometry or not self._shown:
            self._geometry = geometry
            self.root.geometry(f"{card_w}x{card_h}+{x}+{y}")
            if self._win32 is not None:
                self.root.update_idletasks()
                self._win_show(True, geometry)
            elif not self._shown:
                self.root.deiconify()
        self._shown = True

    def _hide(self) -> None:
        if self._win32 is not None:
            self._win_show(False)
        else:
            self.root.withdraw()
        self._shown = False

    def _paint(self, text: str, stable: int, lines: list[tuple[int, int]], w: int, h: int) -> None:
        st = HudStyle
        c = self.canvas
        c.delete("all")
        c.create_rectangle(0, 0, w - 1, h - 1, outline=st.BORDER, fill=st.CARD)
        bx = self._px(st.PAD_X + 1.5)
        cy = self._px(st.PAD_Y) + self._line_h / 2
        bar_w = max(2, self._px(3))
        if self.model.mode == "error":
            r = self._px(3.5)
            c.create_oval(bx + self._px(5) - r, cy - r, bx + self._px(5) + r, cy + r, fill=st.ERROR, outline="")
        else:
            heights = st.bar_heights(self.model, self._line_h / self.scale)
            for i in range(3):
                x = bx + self._px(i * 5)
                if self.model.mode == "finishing":
                    color = st.BAR if st.dot_alpha(self.model, i) > 0.6 else st.BAR_IDLE
                    half = 0.0
                else:
                    color, half = st.BAR, max(0.0, self._px(heights[i]) / 2 - bar_w / 2)
                c.create_line(x, cy - half, x, cy + half + 0.01, width=bar_w, capstyle="round", fill=color)
        tx, ty = self._text_inset(), self._px(st.PAD_Y)
        for row, (a, b) in enumerate(lines):
            y = ty + row * self._line_h
            head = text[a:min(b, max(a, stable))]
            tail = text[max(a, stable):b] if stable < b else ""
            if self.model.mode == "error":
                c.create_text(tx, y, text=text[a:b], anchor="nw", font=self.font, fill=st.ERROR)
                continue
            if head:
                c.create_text(tx, y, text=head, anchor="nw", font=self.font, fill=st.TEXT)
            if tail:
                c.create_text(tx + self._measure(head), y, text=tail, anchor="nw", font=self.font, fill=st.TAIL)


def create_hud(options: Options) -> Any:
    if sys.platform.startswith("linux"):
        hud: Any = GtkHud(options)
        if hud.start():
            return hud
    hud = TkHud(options)
    return hud if hud.start() else None


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
    HEARTBEAT_SEC = 3.0
    READ_TIMEOUT_SEC = 15.0  # 服务端对每个心跳回 pong；这么久收不到任何消息说明连接已死

    def __init__(self, options: Options, hud: Any = None) -> None:
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

    def _hud(self, event: str, text: str = "", stable: int = -1, level: float | None = None) -> None:
        if self.hud is not None:
            self.hud.submit(event, text, stable, level)

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
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                self.sock = None
            if self.stop_event.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    def _session(self) -> None:
        LOG.info("connecting to %s:%s", self.options.server, self.options.port)
        sock = socket.create_connection((self.options.server, self.options.port), timeout=8)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(self.READ_TIMEOUT_SEC)
        self.sock = sock
        self.buffer.clear()
        with self.send_lock:
            sock.sendall(
                encode_message(
                    {"type": "hello", "version": PROTOCOL_VERSION, "role": "agent", "host": self.options.host, "token": self.options.token}
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
            self._handle(read_line(sock, self.buffer))

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "insert_text":
            request_id = str(message.get("request_id", ""))
            text = str(message.get("text", ""))
            if self.dedupe.seen(request_id):
                self.send({"type": "ack", "request_id": request_id, "ok": True, "error": None})
                return
            # 输入法习惯：上屏的同时候选框消失
            self._hud("commit")
            try:
                backend = self.injector.inject(text)
                LOG.info("inserted %d chars via %s", len(text), backend)
                self.send({"type": "ack", "request_id": request_id, "ok": True, "error": None})
            except Exception as exc:  # noqa: BLE001
                LOG.error("inject failed: %s", exc)
                self._hud("error", f"未能输入：{exc}")
                self.send({"type": "ack", "request_id": request_id, "ok": False, "error": str(exc)})
        elif kind == "hud":
            level = message.get("level")
            self._hud(
                str(message.get("event", "")),
                str(message.get("text", "")),
                int(message.get("stable", -1)),
                float(level) if level is not None else None,
            )
        elif kind == "active_state":
            # false：另一台机器才是当前屏幕（本机丢了 leave）；true：本机重启前就是当前屏幕
            active = bool(message.get("active"))
            with self.state_lock:
                changed, self.active = self.active != active, active
            if changed:
                LOG.info("active=%s (from server)", active)
        elif kind == "ping":
            self.send({"type": "pong", "ts": time.time()})
        elif kind not in {"pong", "hello_ack"}:
            LOG.debug("ignored message type: %s", kind)

    def heartbeat(self) -> None:
        while not self.stop_event.wait(self.HEARTBEAT_SEC):
            if self.connected:
                self.send({"type": "heartbeat", "host": self.options.host, "active": self.active, "ts": time.time()})

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


def _windows_session_id() -> int | None:
    session = ctypes.c_ulong(0)
    kernel32 = ctypes.windll.kernel32
    if kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        return int(session.value)
    return None


def _enable_dpi_awareness() -> None:
    """按物理像素工作：光标坐标、窗口位置与字体渲染一致，高 DPI 下不模糊。"""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    os.environ.setdefault("NO_AT_BRIDGE", "1")
    if os.name == "nt":
        # SSH/服务启动的进程在 Session 0：没有桌面，浮窗不可见、SendInput 打不进用户会话，
        # 还会占住单实例锁挡住正常实例。直接拒绝，交给计划任务在用户会话里启动。
        if _windows_session_id() == 0 and not args.dry_run:
            LOG.error("running in Windows session 0 (no desktop); start via the VoiceTunnelAgent task instead")
            return 3
        _enable_dpi_awareness()
    # 单实例锁：占住文件锁，重复启动的实例直接退出（任务计划/脚本清理不可靠）
    lock_handle = _acquire_instance_lock()
    if lock_handle is None:
        LOG.error("another voice-tunnel agent is already running (instance lock busy); exiting")
        return 1
    options = load_options(args)
    if options.token == "change-me":
        LOG.warning("using default token; change it in config.ini")
    if not options.host:
        options.host = platform.node()

    # 先建 Agent 并启动网络/看门狗，HUD 初始化放在最后——个别机器上 Tk/GTK
    # 会卡死（锁屏/会话状态），不能让它阻塞连接与注入
    agent = Agent(options)
    httpd: LocalHttpServer | None = None
    try:
        httpd = LocalHttpServer(("127.0.0.1", options.local_port), agent)
    except OSError as exc:
        LOG.warning("local event endpoint unavailable on %s: %s", options.local_port, exc)

    if httpd is not None:
        threading.Thread(target=httpd.serve_forever, daemon=True, name="local-http").start()
    threading.Thread(target=agent.heartbeat, daemon=True, name="heartbeat").start()
    threading.Thread(target=agent.run_forever, daemon=True, name="network").start()

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

    threading.Thread(target=_startup_watchdog, daemon=True, name="startup-watchdog").start()

    hud = create_hud(options) if options.hud else None
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
        if hud is not None:
            hud.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
