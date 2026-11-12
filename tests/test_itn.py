import unittest

from voice_tunnel.itn import normalize_numbers as n


class ConvertTests(unittest.TestCase):
    def test_digit_by_digit(self) -> None:
        self.assertEqual(n("端口改成八七六五。"), "端口改成8765。")
        self.assertEqual(n("RTX二零六零只有十二G显存"), "RTX2060只有12G显存")
        self.assertEqual(n("会议室是B座三零二"), "会议室是B座302")
        self.assertEqual(n("八张四零九零，每张二十四G"), "八张4090，每张24G")
        self.assertEqual(n("二十四 G"), "24 G")
        self.assertEqual(n("九零后"), "90后")

    def test_decimal_ip_version(self) -> None:
        self.assertEqual(n("到幺九二点幺六八点二点幺三，"), "到192.168.2.13，")
        self.assertEqual(n("升到三点一四"), "升到3.14")
        self.assertEqual(n("USB三点零的转接头"), "USB3.0的转接头")
        self.assertEqual(n("loss 降到零点三五左右"), "loss 降到0.35左右")

    def test_positional(self) -> None:
        self.assertEqual(n("买了一百二十九块钱"), "买了129块钱")
        self.assertEqual(n("跑到两千个 iteration"), "跑到2000个 iteration")
        self.assertEqual(n("改为二百五十毫秒"), "改为250毫秒")
        self.assertEqual(n("一百二和三千五"), "120和3500")
        self.assertEqual(n("一百零五"), "105")
        self.assertEqual(n("五千万和三万"), "5000万和3万")

    def test_percent(self) -> None:
        self.assertEqual(n("提升了百分之五十"), "提升了50%")
        self.assertEqual(n("百分之三点五"), "3.5%")
        self.assertEqual(n("百分之百"), "百分之百")

    def test_cue_words(self) -> None:
        self.assertEqual(n("端口二十二"), "端口22")
        self.assertEqual(n("版本二"), "版本2")


class KeepTests(unittest.TestCase):
    def test_ordinary_chinese_untouched(self) -> None:
        for text in [
            "明天下午三点开组会，大概要讲二十分钟",
            "下周三上午十点预约好了",
            "给我一个苹果，等一下",
            "万一失败了，千万别删",
            "三三两两地走",
            "七八个人",
            "统一标准",
            "一点一点地来",
            "今天三点一刻出发",
            "下午三点二十开会",
            "第二个和第三次",
        ]:
            self.assertEqual(n(text), text, text)

    def test_time_like_decimal_untouched(self) -> None:
        self.assertEqual(n("下午三点零五开会"), "下午三点零五开会")
        self.assertEqual(n("三点零五分"), "三点零五分")

    def test_already_arabic_and_empty(self) -> None:
        self.assertEqual(n("端口 8765，x99"), "端口 8765，x99")
        self.assertEqual(n(""), "")


if __name__ == "__main__":
    unittest.main()
