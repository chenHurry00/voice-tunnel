"""Control-channel client (CLI / listener -> server)."""

from __future__ import annotations

import json
import socket
import threading
from typing import Any

from .config import load_agent_config
from .protocol import PROTOCOL_VERSION, encode_message


class ControlClient:
    """长连接控制通道：线程安全，断线后下一次请求自动重连。"""

    def __init__(self, config_path: str | None, timeout: float = 8.0) -> None:
        cfg = load_agent_config(config_path)
        host = cfg.server
        self.address = ("127.0.0.1" if host in {"0.0.0.0", "::", ""} else host, cfg.port)
        self.token = cfg.token
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        assert self._sock is not None
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("server closed connection")
            self._buffer.extend(chunk)
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer[:] = rest
        return json.loads(line.decode("utf-8"))

    def _connect(self) -> None:
        sock = socket.create_connection(self.address, timeout=self.timeout)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._buffer.clear()
        sock.sendall(encode_message({"type": "hello", "version": PROTOCOL_VERSION, "role": "control", "token": self.token}))
        hello = self._read()
        if not hello.get("ok"):
            self.close()
            raise RuntimeError(f"auth failed: {hello.get('error')}")

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._sock is None:
                        self._connect()
                    assert self._sock is not None
                    self._sock.sendall(encode_message({"type": "control", **message}))
                    return self._read()
                except socket.timeout:
                    # 请求已送达、只是回复慢：服务端可能已执行（如插入文本），重发会重复执行
                    self.close()
                    raise
                except (OSError, ConnectionError, ValueError):
                    # 长连接已失效（服务端重启等）：重连后重发一次
                    self.close()
                    if attempt:
                        raise
        raise RuntimeError("unreachable")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None


def control_request(config_path: str | None, message: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    client = ControlClient(config_path, timeout)
    try:
        return client.request(message)
    finally:
        client.close()
