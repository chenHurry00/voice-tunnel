"""用 Agent 里真实的 HudModel + GtkHud 绘制代码，离屏渲染 README 演示动图的逐帧 PNG。

场景是虚构的终端（不含任何真实屏幕内容）：按住右 Ctrl → 浮窗逐字预览 → 松手上屏。
用法（在仓库根目录、有 DISPLAY 的 Linux 桌面下）：

    python3 scripts/make_demo.py /tmp/vt-frames
    ffmpeg -framerate 25 -i /tmp/vt-frames/f%04d.png -vf "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" docs/images/demo.gif

横幅 docs/images/banner.png 由同目录的 banner.html 渲染：

    google-chrome --headless=new --hide-scrollbars --force-device-scale-factor=2 \\
        --window-size=1200,400 --screenshot=docs/images/banner.png docs/images/banner.html
"""
import importlib.util
import math
import sys
import types
from pathlib import Path

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

spec = importlib.util.spec_from_file_location("vta", "agent/voice_tunnel_agent.py")
agent = importlib.util.module_from_spec(spec)
sys.modules["vta"] = agent
spec.loader.exec_module(agent)

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
FPS = 25
W, H = 880, 400
MONO = "DejaVu Sans Mono, Noto Sans CJK SC"
UI = "Noto Sans CJK SC"

# ---- 时间线（秒）
PRESS, RELEASE, COMMIT, END = 0.9, 4.6, 5.05, 7.2
FINAL = "把 load_config 的返回值改成 dataclass，再补两个单元测试"
PARTIALS = [  # (时刻, 文本, 稳定字数)：模拟流式识别的逐步推进与尾部修正
    (1.25, "把", 0),
    (1.5, "把 load", 1),
    (1.8, "把 load_config 的", 7),
    (2.1, "把 load_config 的返回值", 16),
    (2.4, "把 load_config 的返回值改成", 20),
    (2.7, "把 load_config 的返回值改成 data class", 22),
    (3.0, "把 load_config 的返回值改成 dataclass，", 27),
    (3.3, "把 load_config 的返回值改成 dataclass，再补", 37),
    (3.6, "把 load_config 的返回值改成 dataclass，再补两个单元", 40),
    (3.95, "把 load_config 的返回值改成 dataclass，再补两个单元测试", 44),
]

# ---- 真实浮窗
hud = agent.GtkHud(agent.Options(font=UI))
assert hud.start()
hud.win.show = lambda: None
hud._work_area = lambda x, y: (0, 0, W, H)
anchor = {"rect": None}


def update_anchor(events):
    hud._anchor_rect = anchor["rect"]


hud._update_anchor = update_anchor


def hud_surface():
    """按当前 model 状态绘制浮窗，返回 (surface, x, y)；隐藏时返回 None。"""
    if not hud.model.visible:
        return None
    hud._sync(events=True)
    x, y, w, h = hud._geometry
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    hud._draw(types.SimpleNamespace(get_allocated_width=lambda: w, get_allocated_height=lambda: h), cairo.Context(surf))
    return surf, x, y


# ---- 场景绘制
def rgb(cr, hex_color, a=1.0):
    v = int(hex_color[1:], 16)
    cr.set_source_rgba((v >> 16) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255, a)


def round_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def text(cr, s, x, y, font, size, color, weight="normal", markup=False):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string(f"{font} {size}")
    if weight == "bold":
        desc.set_weight(Pango.Weight.BOLD)
    elif weight == "medium":
        desc.set_weight(Pango.Weight.MEDIUM)
    layout.set_font_description(desc)
    if markup:
        layout.set_markup(s, -1)
    else:
        layout.set_text(s, -1)
    rgb(cr, color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return layout.get_pixel_size()


def shadow(cr, x, y, w, h, r, spread=18, alpha=0.08):
    for k in range(spread, 0, -1):
        cr.set_source_rgba(0.06, 0.09, 0.16, alpha * (1 - k / (spread + 1)) ** 2 / 3)
        round_rect(cr, x - k, y - k + 6, w + 2 * k, h + 2 * k, r + k)
        cr.fill()


WX, WY, WW, WH = 40, 26, 800, 262  # 终端窗口
PROMPT_Y = WY + 190
ROW_Y = WY + WH + 46  # 底部按键/步骤行


def measure(cr, s, font, size, weight="normal"):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string(f"{font} {size}")
    if weight == "medium":
        desc.set_weight(Pango.Weight.MEDIUM)
    layout.set_font_description(desc)
    layout.set_text(s, -1)
    return layout.get_pixel_size()
PROMPT_X = WX + 58


def draw_scene(cr, t, typed, pressed):
    rgb(cr, "#eef0f3")
    cr.paint()

    # 终端窗口
    shadow(cr, WX, WY, WW, WH, 12)
    round_rect(cr, WX, WY, WW, WH, 12)
    rgb(cr, "#1d2026")
    cr.fill()
    round_rect(cr, WX, WY, WW, 38, 12)
    cr.rectangle(WX, WY + 20, WW, 18)
    rgb(cr, "#262a31")
    cr.fill()
    for i, c in enumerate(("#4a505a", "#4a505a", "#4a505a")):
        cr.arc(WX + 22 + i * 18, WY + 19, 5.5, 0, 2 * math.pi)
        rgb(cr, c, 0.85)
        cr.fill()
    text(cr, "workstation-a  ·  ~/projects/demo", WX + WW / 2 - 130, WY + 10, UI, 10, "#8b929c")

    # 之前的会话输出
    lines = [
        ("<span foreground='#7ee787'>✓</span> 已修改 src/config.py <span foreground='#8b929c'>(+12 −4)</span>", 0),
        ("<span foreground='#7ee787'>✓</span> pytest -q  <span foreground='#8b929c'>41 passed in 0.03s</span>", 1),
        ("<span foreground='#8b929c'>下一步要做什么？</span>", 2),
    ]
    for markup, i in lines:
        text(cr, markup, WX + 30, WY + 66 + i * 30, MONO, 12, "#d5d9de", markup=True)

    # 输入框
    bx, by, bw, bh = WX + 24, PROMPT_Y - 14, WW - 48, 50
    round_rect(cr, bx, by, bw, bh, 8)
    rgb(cr, "#3a404a")
    cr.set_line_width(1.2)
    cr.stroke()
    text(cr, "›", WX + 40, PROMPT_Y - 1, MONO, 14, "#79c0ff", weight="bold")
    tw, th = text(cr, typed, PROMPT_X, PROMPT_Y, MONO, 12, "#e6e8eb") if typed else (0, 18)
    caret_x = PROMPT_X + tw + 1
    if int(t * 2.2) % 2 == 0 or pressed:
        cr.rectangle(caret_x, PROMPT_Y + 1, 2, 18)
        rgb(cr, "#e6e8eb")
        cr.fill()
    anchor["rect"] = (int(caret_x), PROMPT_Y + 1, 2, 20)

    # 底部一行：键帽 + 三个步骤，整体居中；宽度按实际文字测量
    step = 0 if t < PRESS else (1 if t < RELEASE else 2)
    labels = ["按住说话", "逐字预览", "松开上屏"]
    sizes = [measure(cr, s, UI, 11, "medium")[0] + 46 for s in labels]
    key_w, gap, row_y = 92, 12, ROW_Y
    total = key_w + 28 + sum(sizes) + gap * (len(labels) - 1)
    x0 = (W - total) / 2

    depth = 1 if pressed else 4
    round_rect(cr, x0, row_y + 4, key_w, 34, 7)
    rgb(cr, "#b9bec6")
    cr.fill()
    round_rect(cr, x0, row_y + 4 - depth, key_w, 34, 7)
    rgb(cr, "#e3e6ea" if pressed else "#ffffff")
    cr.fill_preserve()
    rgb(cr, "#c9cdd3")
    cr.set_line_width(1)
    cr.stroke()
    text(cr, "右", x0 + 10, row_y + 11 - depth, UI, 8, "#8f959e")
    text(cr, "Ctrl", x0 + 34, row_y + 8 - depth, UI, 11, "#1f2329", weight="medium")
    x0 += key_w + 28

    for i, (s, sw) in enumerate(zip(labels, sizes)):
        active = i == step
        round_rect(cr, x0, row_y, sw, 34, 17)
        if active:
            rgb(cr, "#1f2329")
            cr.fill()
            col = "#ffffff"
        else:
            rgb(cr, "#ffffff")
            cr.fill_preserve()
            rgb(cr, "#dcdfe4")
            cr.set_line_width(1)
            cr.stroke()
            col = "#8f959e"
        text(cr, f"{i + 1}", x0 + 15, row_y + 7, UI, 10, col, weight="bold")
        text(cr, s, x0 + 30, row_y + 6, UI, 11, col, weight="medium")
        x0 += sw + gap


# ---- 逐帧推进
events = list(PARTIALS)
frame = 0
t = 0.0
typed = ""
listening = False
step_dt = 1.0 / FPS
while t < END:
    pressed = PRESS <= t < RELEASE
    if pressed and not listening:
        hud.model.apply("listen", level=0.0)
        listening = True
    while events and t >= events[0][0]:
        _, s, stable = events.pop(0)
        hud.model.apply("partial", s, stable, None)
    if pressed:  # 说话音量：起伏的包络
        level = 0.25 + 0.55 * abs(math.sin(t * 5.3)) * (0.6 + 0.4 * math.sin(t * 1.7 + 1))
        if t > 4.05:
            level *= 0.25
        hud.model.apply("level", level=level)
    if listening and t >= RELEASE and hud.model.mode == "listening":
        hud.model.apply("partial", FINAL, len(FINAL), None)
        hud.model.apply("finish")
    if listening and t >= COMMIT:
        hud.model.apply("commit")
        typed = FINAL
        listening = False
    for _ in range(int(round(step_dt / 0.016))):  # 与真实 60fps 动画同速
        hud.model.tick(0.016)

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    draw_scene(cr, t, typed, pressed)
    card = hud_surface()
    if card is not None:
        csurf, cx, cy = card
        cr.set_source_surface(csurf, cx, cy)
        cr.paint()
    surf.write_to_png(str(OUT / f"f{frame:04d}.png"))
    frame += 1
    t += step_dt
print("frames", frame)
