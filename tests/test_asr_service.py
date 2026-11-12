import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "qwen_asr_service", Path(__file__).resolve().parents[1] / "scripts" / "qwen_asr_service.py"
)
service = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = service
_spec.loader.exec_module(service)  # type: ignore[union-attr]

CONTEXT = "程序员日常口述，中英文混合。英文单词、缩写、型号、命令保持原文。设备名：OMEN、yuchen-X99。"


class EchoTests(unittest.TestCase):
    def test_full_and_partial_echo_detected(self) -> None:
        self.assertTrue(service.echoes_context(CONTEXT, CONTEXT))
        self.assertTrue(service.echoes_context("程序员日常口述，中英文混合。英文单词", CONTEXT))
        # 空白差异不影响
        self.assertTrue(service.echoes_context("程序员日常口述， 中英文混合。", CONTEXT))

    def test_echo_with_trailing_hallucination_detected(self) -> None:
        self.assertTrue(service.echoes_context(CONTEXT + "嗯。", CONTEXT))

    def test_real_speech_and_short_words_kept(self) -> None:
        self.assertFalse(service.echoes_context("把 x99 的服务端口改成 8765。", CONTEXT))
        self.assertFalse(service.echoes_context("嗯。", CONTEXT))
        self.assertFalse(service.echoes_context("设备名", CONTEXT))  # 低于长度门槛
        self.assertFalse(service.echoes_context("", CONTEXT))
        self.assertFalse(service.echoes_context("你好，今天天气不错。", ""))


if __name__ == "__main__":
    unittest.main()
