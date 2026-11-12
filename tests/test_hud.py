import importlib.util
import sys
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "voice_tunnel_agent", Path(__file__).resolve().parents[1] / "agent" / "voice_tunnel_agent.py"
)
agent = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = agent  # dataclass 需要模块已注册
_spec.loader.exec_module(agent)  # type: ignore[union-attr]


def run_until_settled(model: "agent.HudModel", frames: int = 60) -> None:
    for _ in range(frames):
        model.tick(0.016)


class HudModelTests(unittest.TestCase):
    def test_new_text_reveals_progressively(self) -> None:
        model = agent.HudModel()
        model.apply("listen", level=0.0)
        model.apply("partial", "今天天气不错", stable=0)
        model.tick(0.016)
        shown, _ = model.display()
        self.assertTrue(0 < len(shown) < 6)
        run_until_settled(model)
        self.assertEqual(model.display()[0], "今天天气不错")

    def test_rewritten_tail_replaced_immediately(self) -> None:
        model = agent.HudModel()
        model.apply("partial", "扫这火今天")
        run_until_settled(model)
        model.apply("partial", "嫂子好今天也", stable=0)
        # 被改写的部分不能继续显示旧字
        self.assertEqual(model.display()[0], "")
        run_until_settled(model)
        self.assertEqual(model.display()[0], "嫂子好今天也")

    def test_commit_hides_and_late_level_does_not_reopen(self) -> None:
        model = agent.HudModel()
        model.apply("partial", "你好")
        model.apply("commit")
        model.apply("level", level=0.5)
        model.apply("finish")
        self.assertFalse(model.visible)

    def test_unknown_event_ignored(self) -> None:
        model = agent.HudModel()
        model.apply("bogus", "x")
        self.assertFalse(model.visible)
        model.apply("partial", "你好")
        model.apply("bogus")
        self.assertTrue(model.visible)

    def test_auto_hide_after_timeout(self) -> None:
        model = agent.HudModel()
        model.apply("error", "未能输入")
        model.deadline = 0
        model.tick(0.016)
        self.assertFalse(model.visible)


class PlacementTests(unittest.TestCase):
    AREA = (0, 0, 1920, 1080)

    def test_card_sits_below_caret_with_text_aligned(self) -> None:
        x, y = agent.place_card((500, 300, 2, 20), (200, 34), 33, self.AREA)
        self.assertEqual((x, y), (500 - 33, 300 + 20 + agent.HudStyle.GAP))

    def test_flips_above_near_bottom(self) -> None:
        _, y = agent.place_card((500, 1060, 2, 18), (200, 34), 33, self.AREA)
        self.assertEqual(y, 1060 - 34 - agent.HudStyle.GAP)

    def test_clamped_to_right_edge(self) -> None:
        x, _ = agent.place_card((1900, 300, 2, 20), (300, 34), 33, self.AREA)
        self.assertEqual(x, 1920 - 300)


if __name__ == "__main__":
    unittest.main()
