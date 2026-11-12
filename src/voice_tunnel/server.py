"""Async TCP server for voice-tunnel."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import ServerConfig
from .protocol import PROTOCOL_VERSION, ProtocolError, encode_message, new_request_id, read_message, write_message

LOG = logging.getLogger("voice_tunnel.server")

# Deskflow 在 Windows 上常出现 leave→enter 抖动（数百毫秒内），leave 后保留原目标一小段时间
LEAVE_GRACE_SEC = 1.5
HUD_EVENTS = {"listen", "level", "partial", "finish", "commit", "error", "hide"}


@dataclass
class AgentConnection:
    host: str
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    last_seen: float = field(default_factory=time.time)
    active: bool = False


class TunnelServer:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.agents: dict[str, AgentConnection] = {}
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.active_host: str | None = None
        self.left_at = 0.0  # active_host 发出 leave 的时刻；0 表示仍在该主机上
        self.seen_event = False  # 收到过任何切屏事件后，目标未知时不再回退 default_target
        self.manual_host: str | None = None
        self.manual_until: float = 0.0
        self._server: asyncio.AbstractServer | None = None

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.config.bind, self.config.port)
        addrs = ", ".join(str(sock.getsockname()) for sock in (self._server.sockets or []))
        LOG.info("voice-tunnel server listening on %s", addrs)
        self._reaper_task = asyncio.create_task(self._reaper())
        try:
            async with self._server:
                await self._server.serve_forever()
        finally:
            self._reaper_task.cancel()

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(2)
            now = time.time()
            if self.manual_host and now >= self.manual_until:
                LOG.info("manual target expired: %s", self.manual_host)
                self.manual_host = None
                self.manual_until = 0.0
            for host, conn in list(self.agents.items()):
                # Agent 每 3s 发心跳；长时间无消息说明连接已死（休眠/断网），主动断开让其重连
                if now - conn.last_seen > self.config.heartbeat_timeout:
                    LOG.warning("agent %s silent for %.0fs, dropping connection", host, now - conn.last_seen)
                    conn.writer.close()

    # ------------------------------------------------------------------ connection

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        role = "unknown"
        host = "?"
        try:
            hello = await asyncio.wait_for(read_message(reader), timeout=5)
            if not hello or hello.get("type") != "hello":
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "hello required"})
                return
            if int(hello.get("version", 0)) != PROTOCOL_VERSION:
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "protocol version mismatch"})
                return
            if not hmac.compare_digest(str(hello.get("token", "")), self.config.token):
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "bad token"})
                LOG.warning("rejected %s: bad token", peer)
                return
            role = str(hello.get("role", "agent"))
            if role == "agent":
                host = str(hello.get("host", "")).strip()
                if not host:
                    await write_message(writer, {"type": "hello_ack", "ok": False, "error": "host required"})
                    return
                await write_message(writer, {"type": "hello_ack", "ok": True})
                await self._agent_loop(host, reader, writer)
            elif role == "control":
                await write_message(writer, {"type": "hello_ack", "ok": True})
                await self._control_loop(reader, writer)
            else:
                await write_message(writer, {"type": "hello_ack", "ok": False, "error": "unknown role"})
        except (ProtocolError, asyncio.TimeoutError) as exc:
            LOG.warning("connection %s/%s: %s", peer, role, exc)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            conn = self.agents.get(host) if role == "agent" else None
            # 只清理属于本连接的登记；重连时旧连接的收尾不能删掉新连接
            if conn is not None and conn.writer is writer:
                # 不清 active_host：Agent 重启/断网重连后鼠标多半还在这台机器上，重连即恢复
                del self.agents[host]
                LOG.info("agent disconnected: %s", host)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass

    async def _agent_loop(self, host: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        old = self.agents.get(host)
        if old is not None:
            LOG.warning("agent %s reconnected, closing old connection", host)
            old.writer.close()
        conn = AgentConnection(host=host, reader=reader, writer=writer)
        self.agents[host] = conn
        LOG.info("agent connected: %s %s", host, writer.get_extra_info("peername"))
        if self.active_host == host and not self.left_at:
            # 重启后的 Agent 不知道自己是当前屏幕，告诉它恢复活跃（继续发活跃心跳）
            conn.active = True
            self._push(conn, {"type": "active_state", "active": True})

        while True:
            message = await read_message(reader)
            if message is None:
                return
            conn.last_seen = time.time()
            kind = message.get("type")
            if kind == "ack":
                request_id = str(message.get("request_id", ""))
                future = self.pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(message)
            elif kind == "active":
                if message.get("state") == "enter":
                    self._on_enter(conn)
                else:
                    self._on_leave(conn)
            elif kind == "heartbeat":
                self._on_heartbeat(conn, bool(message.get("active")))
                self._push(conn, {"type": "pong", "ts": time.time()})
            elif kind == "ping":
                self._push(conn, {"type": "pong", "ts": time.time()})
            else:
                LOG.debug("agent %s: ignored %s", host, kind)

    # ------------------------------------------------------------------ active target

    def _on_enter(self, conn: AgentConnection) -> None:
        self.seen_event = True
        conn.active = True
        if self.active_host != conn.host or self.left_at:
            LOG.info("active target -> %s", conn.host)
        self.active_host = conn.host
        self.left_at = 0.0
        # 同一时刻只有一个鼠标：其他自认为活跃的 Agent（丢了 leave 事件）一律失活
        for other in self.agents.values():
            if other is not conn and other.active:
                other.active = False
                self._push(other, {"type": "active_state", "active": False})

    def _on_leave(self, conn: AgentConnection) -> None:
        self.seen_event = True
        conn.active = False
        if self.active_host == conn.host and not self.left_at:
            self.left_at = time.time()
            LOG.info("left %s", conn.host)

    def _on_heartbeat(self, conn: AgentConnection, active: bool) -> None:
        if not active:
            conn.active = False
            return
        if self.active_host == conn.host and not self.left_at:
            conn.active = True
            return
        if self.current_target() is None:
            LOG.info("recovered active target from heartbeat: %s", conn.host)
            self._on_enter(conn)
        else:
            # 另一台主机已是目标：这台是丢了 leave 的僵尸活跃状态
            conn.active = False
            self._push(conn, {"type": "active_state", "active": False})

    def current_target(self) -> str | None:
        now = time.time()
        if self.manual_host and now < self.manual_until:
            return self.manual_host
        host = self.active_host
        if host and host in self.agents and (not self.left_at or now - self.left_at < LEAVE_GRACE_SEC):
            return host
        if not self.seen_event:
            return self.config.default_target
        return None  # 鼠标在没有 Agent 的屏幕上，或目标 Agent 不在线：不猜

    def _resolve(self, target: str | None) -> AgentConnection:
        name = str(target or "active")
        resolved = self.current_target() if name == "active" else name
        if not resolved:
            raise ValueError("no active target")
        conn = self.agents.get(resolved)
        if conn is None:
            raise ValueError(f"agent not connected: {resolved}")
        return conn

    def _push(self, conn: AgentConnection, message: dict[str, Any]) -> None:
        """非阻塞发送；对端卡死导致写缓冲堆积时断开，交给重连恢复。"""
        try:
            conn.writer.write(encode_message(message))
            transport = conn.writer.transport
            if transport is not None and transport.get_write_buffer_size() > 512 * 1024:
                LOG.warning("agent %s write buffer full, dropping connection", conn.host)
                conn.writer.close()
        except (ConnectionError, RuntimeError) as exc:
            LOG.debug("push to %s failed: %s", conn.host, exc)

    # ------------------------------------------------------------------ control

    async def _control_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            message = await read_message(reader)
            if message is None:
                return
            if message.get("type") != "control":
                await write_message(writer, {"type": "result", "id": message.get("id"), "ok": False, "error": "control required"})
                continue
            command = str(message.get("command", ""))
            request_id = message.get("id")
            try:
                data = await self._dispatch_control(command, message)
                reply: dict[str, Any] = {"type": "result", "id": request_id, "ok": True, "data": data}
            except Exception as exc:  # noqa: BLE001 - report all control errors to CLI
                reply = {"type": "result", "id": request_id, "ok": False, "error": str(exc)}
            await write_message(writer, reply)

    async def _dispatch_control(self, command: str, message: dict[str, Any]) -> Any:
        if command == "send":
            text = str(message.get("text", ""))
            if not text:
                raise ValueError("text is empty")
            if len(text) > 20_000:
                raise ValueError("text too long")
            conn = self._resolve(message.get("target"))
            ack = await self.send_text(conn, text)
            return {"target": conn.host, "request_id": ack.get("request_id"), "ack": ack}
        if command == "resolve":
            return {"target": self.current_target()}
        if command == "hud":
            event = str(message.get("event", ""))
            if event not in HUD_EVENTS:
                raise ValueError(f"unknown hud event: {event}")
            conn = self._resolve(message.get("target"))
            payload: dict[str, Any] = {"type": "hud", "event": event}
            for key in ("text", "stable", "level"):
                if key in message:
                    payload[key] = message[key]
            self._push(conn, payload)
            return {"target": conn.host}
        if command == "targets":
            return [self._agent_info(c) for c in self.agents.values()]
        if command == "status":
            return {
                "active_target": self.current_target(),
                "active_host": self.active_host,
                "default_target": self.config.default_target,
                "manual_host": self.manual_host,
                "agents": [self._agent_info(c) for c in self.agents.values()],
            }
        if command == "set_target":
            host = message.get("host")
            if host:
                self.manual_host = str(host)
                self.manual_until = time.time() + float(message.get("ttl", 600))
                LOG.info("manual target -> %s (ttl %ss)", self.manual_host, message.get("ttl", 600))
            else:
                self.manual_host = None
                self.manual_until = 0.0
                LOG.info("manual target cleared")
            return {"manual_host": self.manual_host}
        raise ValueError(f"unknown command: {command}")

    # ------------------------------------------------------------------ operations

    def _agent_info(self, conn: AgentConnection) -> dict[str, Any]:
        return {
            "host": conn.host,
            "active": conn.active,
            "last_seen": round(time.time() - conn.last_seen, 1),
        }

    async def send_text(self, conn: AgentConnection, text: str) -> dict[str, Any]:
        request_id = new_request_id()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self._push(conn, {"type": "insert_text", "request_id": request_id, "text": text, "created_at": time.time()})
        try:
            ack = await asyncio.wait_for(future, timeout=self.config.ack_timeout)
        except asyncio.TimeoutError as exc:
            self.pending.pop(request_id, None)
            raise ValueError(f"agent {conn.host} did not ack within {self.config.ack_timeout}s") from exc
        if not ack.get("ok"):
            raise ValueError(f"agent {conn.host} rejected text: {ack.get('error')}")
        return {"request_id": request_id, **ack}
