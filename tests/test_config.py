import tempfile
import unittest
from pathlib import Path

from voice_tunnel.config import DEFAULT_ASR_CONTEXT, load_voice_config


class VoiceConfigTests(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text(text, encoding="utf-8")
            return load_voice_config(path)

    def test_defaults(self) -> None:
        config = self.load("[voice]\n")
        self.assertEqual(config.context, DEFAULT_ASR_CONTEXT)
        self.assertEqual(config.qwen_gpu_memory_utilization, 0.75)

    def test_context_may_contain_percent(self) -> None:
        # ConfigParser 默认插值会把 % 当语法，口述上下文里写“50%”不能让监听端起不来
        config = self.load("[voice]\ncontext = 提升了 50% 以上\n")
        self.assertEqual(config.context, "提升了 50% 以上")

    def test_overrides(self) -> None:
        config = self.load("[voice]\nqwen_gpu_memory_utilization = 0.4\ncontext =\n")
        self.assertEqual(config.qwen_gpu_memory_utilization, 0.4)
        self.assertEqual(config.context, "")


if __name__ == "__main__":
    unittest.main()
