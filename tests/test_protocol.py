import json
import unittest

from voice_tunnel.protocol import PROTOCOL_VERSION, ProtocolError, encode_message, new_request_id


class ProtocolTests(unittest.TestCase):
    def test_encode_is_newline_delimited_json(self) -> None:
        raw = encode_message({"type": "hello", "version": PROTOCOL_VERSION})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(json.loads(raw.decode("utf-8")), {"type": "hello", "version": PROTOCOL_VERSION})

    def test_unicode_is_preserved(self) -> None:
        raw = encode_message({"type": "insert_text", "text": "中文 ABC_123。"})
        self.assertIn("中文 ABC_123。", raw.decode("utf-8"))

    def test_request_id_is_unique(self) -> None:
        self.assertNotEqual(new_request_id(), new_request_id())

    def test_oversized_message_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_message({"type": "insert_text", "text": "x" * (1024 * 1024)})


if __name__ == "__main__":
    unittest.main()
