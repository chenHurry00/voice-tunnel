import json
import time
import unittest
from unittest import mock

from voice_tunnel import server as server_mod
from voice_tunnel.config import ServerConfig
from voice_tunnel.server import AgentConnection, TunnelServer


class FakeTransport:
    def get_write_buffer_size(self) -> int:
        return 0


class FakeWriter:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.closed = False
        self.transport = FakeTransport()

    def write(self, data: bytes) -> None:
        self.messages.append(json.loads(data))

    def close(self) -> None:
        self.closed = True


def make_server() -> TunnelServer:
    return TunnelServer(ServerConfig(default_target="yuchen-X99"))


def connect(server: TunnelServer, host: str) -> AgentConnection:
    conn = AgentConnection(host=host, reader=None, writer=FakeWriter())  # type: ignore[arg-type]
    server.agents[host] = conn
    return conn


class ActiveTargetTests(unittest.TestCase):
    def test_default_target_only_before_any_event(self) -> None:
        server = make_server()
        x99 = connect(server, "yuchen-X99")
        self.assertEqual(server.current_target(), "yuchen-X99")
        server._on_leave(x99)
        # 已经出现过切屏事件：鼠标去了没有 Agent 的屏幕，不能再回退到 X99
        with mock.patch.object(server_mod.time, "time", return_value=time.time() + 5):
            self.assertIsNone(server.current_target())

    def test_enter_deactivates_other_agents(self) -> None:
        server = make_server()
        omen = connect(server, "OMEN")
        x99 = connect(server, "yuchen-X99")
        server._on_enter(omen)
        server._on_enter(x99)  # OMEN 的 leave 丢了
        self.assertEqual(server.current_target(), "yuchen-X99")
        self.assertFalse(omen.active)
        self.assertIn({"type": "active_state", "active": False}, omen.writer.messages)

    def test_leave_grace_survives_flicker(self) -> None:
        server = make_server()
        omen = connect(server, "OMEN")
        server._on_enter(omen)
        server._on_leave(omen)
        self.assertEqual(server.current_target(), "OMEN")  # 宽限期内仍是 OMEN
        server._on_enter(omen)
        with mock.patch.object(server_mod.time, "time", return_value=time.time() + 5):
            self.assertEqual(server.current_target(), "OMEN")

    def test_zombie_heartbeat_is_told_to_deactivate(self) -> None:
        server = make_server()
        omen = connect(server, "OMEN")
        x99 = connect(server, "yuchen-X99")
        server._on_enter(x99)
        server._on_heartbeat(omen, True)
        self.assertEqual(server.current_target(), "yuchen-X99")
        self.assertIn({"type": "active_state", "active": False}, omen.writer.messages)

    def test_heartbeat_recovers_lost_enter(self) -> None:
        server = make_server()
        x99 = connect(server, "yuchen-X99")
        omen = connect(server, "OMEN")
        server._on_enter(x99)
        server._on_leave(x99)
        with mock.patch.object(server_mod.time, "time", return_value=time.time() + 5):
            server._on_heartbeat(omen, True)
            self.assertEqual(server.current_target(), "OMEN")

    def test_disconnected_target_is_not_resolved(self) -> None:
        server = make_server()
        omen = connect(server, "OMEN")
        server._on_enter(omen)
        del server.agents["OMEN"]
        self.assertIsNone(server.current_target())
        with self.assertRaises(ValueError):
            server._resolve("active")
        # 同一台机器重连（Agent 重启）后恢复为目标
        connect(server, "OMEN")
        self.assertEqual(server.current_target(), "OMEN")


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_hud_event_is_forwarded_to_explicit_target(self) -> None:
        server = make_server()
        omen = connect(server, "OMEN")
        result = await server._dispatch_control("hud", {"event": "partial", "target": "OMEN", "text": "你好", "stable": 1})
        self.assertEqual(result, {"target": "OMEN"})
        self.assertEqual(omen.writer.messages[-1], {"type": "hud", "event": "partial", "text": "你好", "stable": 1})

    async def test_unknown_hud_event_rejected(self) -> None:
        server = make_server()
        connect(server, "OMEN")
        with self.assertRaises(ValueError):
            await server._dispatch_control("hud", {"event": "boom", "target": "OMEN"})


if __name__ == "__main__":
    unittest.main()
