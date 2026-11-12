"""Configuration loading for voice-tunnel (stdlib INI)."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG = Path("config.ini")


@dataclass
class ServerConfig:
    bind: str = "0.0.0.0"
    port: int = 8765
    token: str = "change-me"
    default_target: str | None = "yuchen-X99"
    heartbeat_timeout: float = 15.0
    ack_timeout: float = 5.0


@dataclass
class VoiceConfig:
    device: str = "USBZH5XK"
    sample_rate: int = 16000
    channels: int = 1
    backend: str = "whisper"
    qwen_backend: str = "transformers"
    qwen_url: str = "http://127.0.0.1:8770/transcribe"
    data_dir: str = "~/.cache/voice-tunnel"
    ptt_key: str = "KEY_RIGHTCTRL"
    ptt_device: str = "/dev/input/by-id/usb-ROYUAN_Eopmaker_Keyboard-event-kbd"
    model: str = "small"
    partial: bool = True
    partial_model: str = "tiny"
    partial_interval: float = 0.6
    language: str = "zh"
    simplify: bool = True
    device_type: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    target: str = "active"
    min_seconds: float = 0.3
    max_seconds: float = 30.0
    download_root: str = ""


@dataclass
class AgentConfig:
    server: str = "192.168.2.134"
    port: int = 8765
    token: str = "change-me"
    host: str = ""
    local_port: int = 8766
    hud: bool = True
    allow_clipboard_fallback: bool = False
    font_linux: str = "Noto Sans CJK SC"
    font_windows: str = "Microsoft YaHei UI"
    max_chars: int = 240
    offset_x: int = 18
    offset_y: int = 22


def _get(parser: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    if parser.has_section(section) and parser.has_option(section, key):
        return parser.get(section, key)
    return default


def _get_bool(parser: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    raw = _get(parser, section, key, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def load_server_config(path: str | Path | None = None) -> ServerConfig:
    parser = configparser.ConfigParser()
    path = Path(path) if path else DEFAULT_CONFIG
    if path.exists():
        parser.read(path, encoding="utf-8")
    return ServerConfig(
        bind=_get(parser, "server", "bind", "0.0.0.0"),
        port=int(_get(parser, "server", "port", "8765")),
        token=_get(parser, "server", "token", "change-me"),
        default_target=_get(parser, "server", "default_target", "yuchen-X99") or None,
        heartbeat_timeout=float(_get(parser, "server", "heartbeat_timeout", "15")),
        ack_timeout=float(_get(parser, "server", "ack_timeout", "5")),
    )


def load_agent_config(path: str | Path | None = None) -> AgentConfig:
    parser = configparser.ConfigParser()
    path = Path(path) if path else DEFAULT_CONFIG
    if path.exists():
        parser.read(path, encoding="utf-8")
    return AgentConfig(
        server=_get(parser, "agent", "server", "192.168.2.134"),
        port=int(_get(parser, "agent", "port", "8765")),
        token=_get(parser, "agent", "token", "change-me"),
        host=_get(parser, "agent", "host", ""),
        local_port=int(_get(parser, "agent", "local_port", "8766")),
        hud=_get_bool(parser, "agent", "hud", True),
        allow_clipboard_fallback=_get_bool(parser, "agent", "allow_clipboard_fallback", False),
        font_linux=_get(parser, "hud", "font_linux", "Noto Sans CJK SC"),
        font_windows=_get(parser, "hud", "font_windows", "Microsoft YaHei UI"),
        max_chars=int(_get(parser, "hud", "max_chars", "240")),
        offset_x=int(_get(parser, "hud", "offset_x", "18")),
        offset_y=int(_get(parser, "hud", "offset_y", "22")),
    )


def load_voice_config(path: str | Path | None = None) -> VoiceConfig:
    parser = configparser.ConfigParser()
    path = Path(path) if path else DEFAULT_CONFIG
    if path.exists():
        parser.read(path, encoding="utf-8")
    return VoiceConfig(
        device=_get(parser, "voice", "device", "USBZH5XK"),
        sample_rate=int(_get(parser, "voice", "sample_rate", "16000")),
        channels=int(_get(parser, "voice", "channels", "1")),
        backend=_get(parser, "voice", "backend", "whisper"),
        qwen_backend=_get(parser, "voice", "qwen_backend", "transformers"),
        qwen_url=_get(parser, "voice", "qwen_url", "http://127.0.0.1:8770/transcribe"),
        data_dir=_get(parser, "voice", "data_dir", "~/.cache/voice-tunnel"),
        ptt_key=_get(parser, "voice", "ptt_key", "KEY_RIGHTCTRL"),
        ptt_device=_get(parser, "voice", "ptt_device", "/dev/input/by-id/usb-ROYUAN_Eopmaker_Keyboard-event-kbd"),
        model=_get(parser, "voice", "model", "small"),
        partial=_get_bool(parser, "voice", "partial", True),
        partial_model=_get(parser, "voice", "partial_model", "tiny"),
        partial_interval=float(_get(parser, "voice", "partial_interval", "0.6")),
        language=_get(parser, "voice", "language", "zh"),
        simplify=_get_bool(parser, "voice", "simplify", True),
        device_type=_get(parser, "voice", "device_type", "cpu"),
        compute_type=_get(parser, "voice", "compute_type", "int8"),
        beam_size=int(_get(parser, "voice", "beam_size", "5")),
        target=_get(parser, "voice", "target", "active"),
        min_seconds=float(_get(parser, "voice", "min_seconds", "0.3")),
        max_seconds=float(_get(parser, "voice", "max_seconds", "30")),
        download_root=_get(parser, "voice", "download_root", ""),
    )
