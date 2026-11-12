"""Command line entry point for voice-tunnel."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Any

from .config import load_agent_config, load_server_config
from .protocol import PROTOCOL_VERSION, encode_message
from .server import TunnelServer


def _read_line(sock: socket.socket, buffer: bytearray) -> dict[str, Any]:
    while b"\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("server closed connection")
        buffer.extend(chunk)
    line, _, rest = buffer.partition(b"\n")
    buffer[:] = rest
    return json.loads(line.decode("utf-8"))


def control_request(config_path: str | None, message: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    cfg = load_agent_config(config_path)
    host = cfg.server
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    with socket.create_connection((host, cfg.port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        buffer = bytearray()
        sock.sendall(
            encode_message(
                {
                    "type": "hello",
                    "version": PROTOCOL_VERSION,
                    "role": "control",
                    "token": cfg.token,
                }
            )
        )
        hello = _read_line(sock, buffer)
        if not hello.get("ok"):
            raise RuntimeError(f"auth failed: {hello.get('error')}")
        sock.sendall(encode_message({"type": "control", **message}))
        return _read_line(sock, buffer)


def _print_result(result: dict[str, Any]) -> int:
    if not result.get("ok"):
        print(f"error: {result.get('error')}", file=sys.stderr)
        return 1
    data = result.get("data")
    if isinstance(data, dict) and "ack" in data:
        ack = data["ack"]
        print(f"inserted into {data.get('target')} (request_id={ack.get('request_id')})")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = TunnelServer(load_server_config(args.config))
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    from .config import load_voice_config
    from .listen import VoiceListener

    config_path = str(Path(args.config).resolve())
    listener = VoiceListener(load_voice_config(args.config), config_path)
    listener.run()
    return 0


def cmd_keytest(args: argparse.Namespace) -> int:
    from .config import load_voice_config
    from .listen import keytest

    keytest(load_voice_config(args.config), seconds=args.seconds, once=args.once)
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    text = args.text
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
    if not text:
        print("error: text is empty", file=sys.stderr)
        return 2
    result = control_request(args.config, {"command": "send", "target": args.target, "text": text})
    return _print_result(result)


def cmd_targets(args: argparse.Namespace) -> int:
    result = control_request(args.config, {"command": "targets"})
    if not result.get("ok"):
        return _print_result(result)
    rows = result.get("data") or []
    if not rows:
        print("no agents connected")
        return 0
    print(f"{'HOST':28} {'ACTIVE':7} LAST_SEEN")
    for row in rows:
        print(f"{row['host']:28} {str(row['active']):7} {row['last_seen']}s")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    result = control_request(args.config, {"command": "status"})
    if not result.get("ok"):
        return _print_result(result)
    print(json.dumps(result.get("data"), ensure_ascii=False, indent=2))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    result = control_request(
        args.config,
        {"command": "preview", "target": args.target, "text": args.text, "stable_len": args.stable_len},
    )
    return _print_result(result)


def cmd_final(args: argparse.Namespace) -> int:
    result = control_request(args.config, {"command": "final", "target": args.target, "text": args.text})
    return _print_result(result)


def cmd_target(args: argparse.Namespace) -> int:
    message: dict[str, Any] = {"command": "set_target", "host": args.host}
    result = control_request(args.config, message)
    if not result.get("ok"):
        return _print_result(result)
    print(f"manual target: {result['data'].get('manual_host') or '(cleared)'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voice-tunnel", description="Centralized voice text tunnel")
    parser.add_argument("--config", default="config.ini", help="path to INI config (default: config.ini)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the TCP server")
    serve.set_defaults(func=cmd_serve)

    listen = sub.add_parser("listen", help="push-to-talk microphone -> faster-whisper")
    listen.set_defaults(func=cmd_listen)

    keytest = sub.add_parser("keytest", help="print evdev key names for PTT selection")
    keytest.add_argument("--seconds", type=float, default=20.0)
    keytest.add_argument("--once", action="store_true", help="exit after the first key press")
    keytest.set_defaults(func=cmd_keytest)

    send = sub.add_parser("send", help="send text to an agent")
    send.add_argument("--target", default="active", help="agent host or 'active'")
    send.add_argument("--text", default="", help="text to insert")
    send.add_argument("--file", help="read text from a UTF-8 file")
    send.set_defaults(func=cmd_send)

    targets = sub.add_parser("targets", help="list connected agents")
    targets.set_defaults(func=cmd_targets)

    status = sub.add_parser("status", help="show active target and agents")
    status.set_defaults(func=cmd_status)

    preview = sub.add_parser("preview", help="send partial text to the HUD (testing)")
    preview.add_argument("--target", default="active")
    preview.add_argument("--text", required=True)
    preview.add_argument("--stable-len", type=int, default=-1)
    preview.set_defaults(func=cmd_preview)

    final = sub.add_parser("final", help="send final text to the HUD (testing)")
    final.add_argument("--target", default="active")
    final.add_argument("--text", required=True)
    final.set_defaults(func=cmd_final)

    target = sub.add_parser("target", help="set or clear manual active target")
    target.add_argument("--host", help="host to pin; omit to clear")
    target.set_defaults(func=cmd_target)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
