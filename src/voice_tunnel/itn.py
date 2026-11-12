"""口述文本的数字规范化：中文数字 → 阿拉伯数字。

只转换口述里几乎没有歧义的几类，宁缺毋滥（“一个”“三点开会”“万一”保持原样）：
- 逐位念的数字串：八七六五、二零六零、幺九二（≥3 位；2 位须含零/幺，或紧邻英文/数字）
- 小数、IP、版本号：三点一四、幺九二点幺六八点二点幺三
- 带百/千/万/亿的数：一百二十九、两千、五千万
- 百分之 X：百分之五十 → 50%
- 紧邻英文字母的数：十二G、四B、RTX二零六零；以及“端口/版本…”之后的数
"""

from __future__ import annotations

import re

_DIGIT = {"零": 0, "〇": 0, "幺": 1, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_DIGITS = "零〇幺一二三四五六七八九"  # 可逐位念的字符（不含“两”）
_UNIT = {"十": 10, "百": 100, "千": 1000}
_BIG = {"万": 10**4, "亿": 10**8}
_NUM = r"[零〇幺一二三四五六七八九两十百千万亿]+(?:点[零〇幺一二三四五六七八九]+)*"
_RUN = re.compile(_NUM)
_PERCENT = re.compile("百分之(" + _NUM + ")")
_QUANT = "个下些次种边点"  # 单个数字后接这些字，多半是普通量词用法（一个、一下）
_CUES = ("端口", "版本", "编号", "型号", "号码")  # 这些词后面的数字几乎都是标识符
_AFTER_DECIMAL = "点分刻半整钟十百千万亿"  # 小数后接这些字，说明其实是时间或更长的数（三点一刻、三点二十）


def _parse(text: str) -> int | None:
    """解析中文位值数（一百二十九、两千零五、三千五），不合法返回 None。"""
    total = section = 0
    num: int | None = None
    last_unit = 0
    zero = False
    for ch in text:
        if ch in "零〇":
            zero = True
        elif ch in _DIGIT:
            if num is not None:
                return None
            num = _DIGIT[ch]
        elif ch in _UNIT:
            if num is None and ch != "十":
                return None
            section += (1 if num is None else num) * _UNIT[ch]
            num, last_unit, zero = None, _UNIT[ch], False
        else:  # 万 / 亿
            section += num or 0
            if section == 0:
                return None
            total += section * _BIG[ch]
            section, num, last_unit, zero = 0, None, 0, False
    if num is not None:
        # 口语省略：一百二 = 120，三千五 = 3500
        section += num * last_unit // 10 if last_unit >= 100 and not zero else num
    return total + section


def _to_arabic(run: str) -> str | None:
    parts = run.split("点")
    out = []
    for index, part in enumerate(parts):
        if all(ch in _DIGITS for ch in part):
            out.append("".join(str(_DIGIT[ch]) for ch in part))
        elif index == 0 and len(parts) <= 2:
            value = _parse(part)
            if value is None:
                return None
            out.append(str(value))
        else:
            return None
    if len(parts) == 1 and not all(ch in _DIGITS for ch in run):
        value = int(out[0])
        big = _BIG.get(run[-1])
        if big and value % big == 0 and value // big < 10000:
            return f"{value // big}{run[-1]}"
    return ".".join(out)


def _latin(ch: str) -> bool:
    return bool(ch) and ch.isascii() and ch.isalnum()


def _wanted(run: str, text: str, start: int, end: int) -> bool:
    prev = text[start - 1] if start else ""
    nxt = text[end] if end < len(text) else ""
    direct = _latin(prev) or _latin(nxt)
    spaced = (prev == " " and _latin(text[start - 2 : start - 1])) or (nxt == " " and _latin(text[end + 1 : end + 2]))
    near = direct or spaced or text[:start].endswith(_CUES)
    parts = run.split("点")
    if len(parts) > 1:
        if nxt and nxt in _AFTER_DECIMAL:
            return False
        head = _to_arabic(parts[0])
        if head is None:
            return False
        # 三点零五 更像 3:05 而不是 3.05
        if len(parts) == 2 and len(parts[1]) == 2 and parts[1][0] in "零〇" and head.isdigit() and int(head) <= 24:
            return False
        return True
    if all(ch in _DIGITS for ch in run):
        size = len(run)
        if size >= 3:
            return True
        if size == 2:
            return near or any(ch in "零〇幺" for ch in run)
        return near and (direct or text[:start].endswith(_CUES)) and not (nxt and nxt in _QUANT)
    if not any(ch in _UNIT or ch in _BIG for ch in run):
        return False  # 只有“两”之类，如 两个
    big = any(ch in "百千万亿" for ch in run) and run[0] in "一二三四五六七八九两"
    return big or near


def normalize_numbers(text: str) -> str:
    if not text:
        return text

    def percent(match: re.Match[str]) -> str:
        value = _to_arabic(match.group(1))
        return f"{value}%" if value else match.group(0)

    text = _PERCENT.sub(percent, text)
    out: list[str] = []
    last = 0
    for match in _RUN.finditer(text):
        run = match.group(0)
        value = _to_arabic(run) if _wanted(run, text, match.start(), match.end()) else None
        if value is not None:
            out.append(text[last : match.start()])
            out.append(value)
            last = match.end()
    out.append(text[last:])
    return "".join(out)
