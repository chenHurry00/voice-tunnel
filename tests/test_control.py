import socket
import threading
import unittest

from voice_tunnel.control import ControlClient


class FakeSock:
    def __init__(self, recv_effect) -> None:
        self.sent: list[bytes] = []
        self.recv_effect = recv_effect

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, _n: int) -> bytes:
        return self.recv_effect()

    def close(self) -> None:
        pass


def make_client(socks: list) -> ControlClient:
    client = ControlClient.__new__(ControlClient)
    client._sock, client._buffer, client._lock = None, bytearray(), threading.Lock()

    def connect() -> None:
        client._sock = socks.pop(0)

    client._connect = connect  # type: ignore[method-assign]
    return client


class ControlRetryTests(unittest.TestCase):
    def test_stale_connection_is_retried_once(self) -> None:
        dead = FakeSock(lambda: b"")  # 服务端已重启：对端关闭
        live = FakeSock(lambda: b'{"type":"result","ok":true}\n')
        client = make_client([dead, live])
        self.assertTrue(client.request({"command": "send"})["ok"])
        self.assertEqual(len(live.sent), 1)

    def test_timeout_is_not_resent(self) -> None:
        def slow() -> bytes:
            raise socket.timeout("timed out")

        first = FakeSock(slow)
        second = FakeSock(lambda: b'{"ok":true}\n')
        client = make_client([first, second])
        with self.assertRaises(socket.timeout):
            client.request({"command": "send", "text": "只插入一次"})
        self.assertEqual(len(second.sent), 0)  # 不能重发，否则文本会被插入两次


if __name__ == "__main__":
    unittest.main()
