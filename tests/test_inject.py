import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "voice_tunnel_agent", Path(__file__).resolve().parents[1] / "agent" / "voice_tunnel_agent.py"
)
agent = sys.modules.get("voice_tunnel_agent") or importlib.util.module_from_spec(_spec)
if "voice_tunnel_agent" not in sys.modules:
    sys.modules[_spec.name] = agent
    _spec.loader.exec_module(agent)  # type: ignore[union-attr]


def chord_for(window_class: str) -> str:
    injector = agent.Injector(agent.Options())
    with mock.patch.object(injector, "_active_window_class", return_value=window_class):
        return injector._paste_chord()


class PasteChordTests(unittest.TestCase):
    def test_terminals_use_ctrl_shift_v(self) -> None:
        # WM_CLASS 的 class 部分（_active_window_class 取最后一项并转小写）
        for window_class in [
            "gnome-terminal-server",
            "terminator",
            "x-terminal-emulator",  # 经 Ctrl+Alt+T / x-terminal-emulator 启动的 terminator
            "xfce4-terminal",
            "konsole",
            "xterm",
            "kitty",
            "tilix",
        ]:
            self.assertEqual(chord_for(window_class), "ctrl+shift+v", window_class)

    def test_other_windows_use_ctrl_v(self) -> None:
        for window_class in ["code", "google-chrome", "gedit", "wechat", ""]:
            self.assertEqual(chord_for(window_class), "ctrl+v", window_class)


if __name__ == "__main__":
    unittest.main()
