import sys
import types
import unittest
from unittest import mock

from voice_tunnel.config import VoiceConfig
from voice_tunnel.listen import AudioRecorder

USB = {"name": "USB Audio: - (hw:1,0)", "max_input_channels": 2}
DEFAULT = {"name": "default", "max_input_channels": 32}


class FakeStream:
    def __init__(self, device, **kwargs):
        self.device = device

    def start(self):
        pass

    def close(self):
        pass


def fake_sounddevice(state: dict) -> types.ModuleType:
    """state['devices']：当前枚举到的设备；state['visible']：重新枚举后可见的设备；state['busy']：打不开的设备。"""
    sd = types.ModuleType("sounddevice")
    sd.query_devices = lambda: list(state["devices"])

    def rescan():
        state["rescans"] += 1
        state["devices"] = list(state["visible"])

    def open_stream(device=None, **kwargs):
        name = "default" if device is None else state["devices"][device]["name"]
        if name in state["busy"]:
            raise RuntimeError("Device unavailable [PaErrorCode -9985]")
        return FakeStream(name, **kwargs)

    sd._terminate = lambda: None
    sd._initialize = rescan
    sd.InputStream = open_stream
    return sd


class InputDeviceFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {"devices": [DEFAULT, USB], "visible": [DEFAULT, USB], "busy": set(), "rescans": 0}
        patcher = mock.patch.dict(sys.modules, {"sounddevice": fake_sounddevice(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recorder = AudioRecorder(VoiceConfig(device="USB Audio"))

    def test_configured_device_used_without_rescan(self) -> None:
        self.recorder.start()
        self.assertEqual(self.recorder.stream.device, USB["name"])
        self.assertEqual(self.state["rescans"], 0)
        self.assertFalse(self.recorder.fallback)

    def test_busy_device_falls_back_to_default(self) -> None:
        # 被 PulseAudio 独占：重新枚举时 PortAudio 看不到它，原索引也打不开
        self.state["busy"] = {USB["name"]}
        self.state["visible"] = [DEFAULT]
        with self.assertLogs("voice_tunnel.listen", "WARNING"):
            self.recorder.start()
        self.assertEqual(self.recorder.stream.device, "default")
        self.assertTrue(self.recorder.fallback)
        self.assertEqual(self.state["rescans"], 1)

    def test_device_restored_on_next_press(self) -> None:
        self.state["busy"] = {USB["name"]}
        self.state["visible"] = [DEFAULT]
        with self.assertLogs("voice_tunnel.listen", "WARNING"):
            self.recorder.start()
        self.recorder.stop()
        self.state["busy"] = set()
        self.state["visible"] = [DEFAULT, USB]
        self.recorder.start()
        self.assertEqual(self.recorder.stream.device, USB["name"])
        self.assertFalse(self.recorder.fallback)

    def test_everything_unavailable_raises(self) -> None:
        self.state["busy"] = {USB["name"], "default"}
        with self.assertRaises(RuntimeError) as ctx:
            self.recorder.start()
        # 报错要同时带上配置设备和默认设备各自的失败原因
        self.assertIn("USB Audio", str(ctx.exception))
        self.assertIn("system default", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
