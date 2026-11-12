import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voice_tunnel import listen
from voice_tunnel.config import VoiceConfig, has_qwen_weights


def make_data_dir(root: Path, models: dict[str, list[str]]) -> None:
    python = root / "venvs" / "qwen-vllm" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    for name, files in models.items():
        (root / "models" / name).mkdir(parents=True)
        for file in files:
            (root / "models" / name / file).touch()


def run_ensure(config: VoiceConfig) -> list[str]:
    """跑一遍自动拉起，返回传给 Popen 的命令行（服务启动用假的 Popen / 健康检查代替）。"""
    health = iter([False, True])
    with mock.patch.object(listen, "_qwen_health", lambda *a, **k: next(health)), mock.patch.object(
        listen.subprocess, "Popen"
    ) as popen:
        listen.ensure_qwen_service(config)
    return popen.call_args.args[0]


class WeightsTests(unittest.TestCase):
    def test_single_and_sharded_weights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data_dir(root, {"single": ["model.safetensors"], "sharded": ["model.safetensors.index.json"], "empty": []})
            self.assertTrue(has_qwen_weights(root / "models" / "single"))
            self.assertTrue(has_qwen_weights(root / "models" / "sharded"))
            self.assertFalse(has_qwen_weights(root / "models" / "empty"))


class AutoStartTests(unittest.TestCase):
    def config(self, root: Path, model: str, **kwargs: object) -> VoiceConfig:
        return VoiceConfig(
            backend="qwen3",
            qwen_backend="vllm",
            qwen_url="http://127.0.0.1:18771/transcribe",
            data_dir=str(root),
            model=str(root / "models" / model),
            device_type="cuda",
            **kwargs,  # type: ignore[arg-type]
        )

    def test_sharded_model_is_used_with_configured_gpu_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data_dir(root, {"Qwen3-ASR-1.7B": ["model.safetensors.index.json"], "Qwen3-ASR-0.6B": ["model.safetensors"]})
            cmd = run_ensure(self.config(root, "Qwen3-ASR-1.7B"))
            self.assertEqual(cmd[cmd.index("--model") + 1], str(root / "models" / "Qwen3-ASR-1.7B"))
            self.assertEqual(cmd[cmd.index("--gpu-memory-utilization") + 1], "0.75")

    def test_gpu_memory_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data_dir(root, {"Qwen3-ASR-0.6B": ["model.safetensors"]})
            cmd = run_ensure(self.config(root, "Qwen3-ASR-0.6B", qwen_gpu_memory_utilization=0.4))
            self.assertEqual(cmd[cmd.index("--gpu-memory-utilization") + 1], "0.4")

    def test_missing_model_falls_back_to_default_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_data_dir(root, {"Qwen3-ASR-0.6B": ["model.safetensors"]})
            cmd = run_ensure(self.config(root, "does-not-exist"))
            self.assertEqual(cmd[cmd.index("--model") + 1], str(root / "models" / "Qwen3-ASR-0.6B"))

    def test_transformers_backend_has_no_gpu_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            python = root / "venvs" / "qwen" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            (root / "models" / "Qwen3-ASR-0.6B").mkdir(parents=True)
            (root / "models" / "Qwen3-ASR-0.6B" / "model.safetensors").touch()
            config = self.config(root, "Qwen3-ASR-0.6B")
            config.qwen_backend = "transformers"
            self.assertNotIn("--gpu-memory-utilization", run_ensure(config))


if __name__ == "__main__":
    unittest.main()
