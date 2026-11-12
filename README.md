# voice-tunnel

集中式语音识别 + 分布式文本注入，用于多主机 vibe coding。

- **语音主机**：X99（Ubuntu X11，RTX 2060 12GB）负责麦克风、PTT、ASR、术语修正和文本分发。
- **目标主机**：每台 Ubuntu / Windows 跑一个轻量 Agent，把文本插入当前焦点窗口。
- **目标跟随**：Deskflow 的 `enterCommand` / `exitCommand` 通知本机 Agent，Agent 用心跳维护 `active_target`。
- **不抢剪贴板**：默认走本机输入接口；Windows 用 Unicode `SendInput`；Linux 用 XTest/xdotool，中文失败时才按需启用剪贴板回退。

当前阶段：**按住说话 → 流式字幕浮窗（Windows 跟随输入光标，Ubuntu 跟随鼠标）→ 松手上屏**，四台机器（X99 / X670 / MetawiseBook / OMEN）均已部署。

![HUD preview](docs/images/hud-preview.png)

浮窗状态从上到下：静音聆听、识别中（浅灰为尚未稳定的尾字）、长句换行、松手后整段校正、上屏失败提示。

## 目录

```text
src/voice_tunnel/          服务端、协议、CLI
agent/voice_tunnel_agent.py  单文件 Agent（stdlib-only）
config.example.ini         配置示例
docs/                      架构、协议、实施状态
```

## 快速开始

```bash
cd /home/yuchen/Sata-sun/Scripts/voice-tunnel
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.ini config.ini   # 修改 token / server 地址

# 终端 1：服务端
voice-tunnel serve --config config.ini

# 终端 2：本机 Agent（dry-run，不真正注入）
python3 agent/voice_tunnel_agent.py --config config.ini --host yuchen-X99 --dry-run

# 终端 3：发一段文本
voice-tunnel send --config config.ini --target yuchen-X99 --text "hello 语音隧道"
```

HUD 预览联调（不需要 ASR）：

```bash
voice-tunnel preview --target yuchen-X99 --text "这是已经识别的部分" --stable 4   # 前 4 字深色，其余浅灰
voice-tunnel final   --target yuchen-X99                                          # 收起浮窗
```

Agent 的健康检查：

```bash
curl 'http://127.0.0.1:8766/health'
curl -X POST 'http://127.0.0.1:8766/active?state=enter'
curl -X POST 'http://127.0.0.1:8766/active?state=leave'
```

## 语音输入

```bash
# 1) PTT 设备权限（X99 键盘 3151:4015；已配置可跳过）
# /etc/udev/rules.d/99-voice-tunnel.rules:
# SUBSYSTEM=="input", KERNEL=="event*", ATTRS{idVendor}=="3151", ATTRS{idProduct}=="4015", TAG+="uaccess", RUN+="/usr/bin/setfacl -m u:yuchen:rw $devnode"

# 2) 安装语音依赖（使用本地镜像）
.venv/bin/pip install -e ".[voice]" -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com

# 3) 启动监听（按住右 Ctrl 说话，松开自动识别并插入当前 active target）
.venv/bin/voice-tunnel --config config.ini listen

# 4) 不知道键名时
.venv/bin/voice-tunnel --config config.ini keytest
```

当前默认：`ptt_key=KEY_RIGHTCTRL`，`model=faster-whisper small`（最终识别），`partial_model=faster-whisper small`（说话中预览），GPU float16；识别结果默认转简体（`zhconv`）。

### 安装位置

模型、虚拟环境、日志统一放在 `[voice] data_dir`（默认 `~/.cache/voice-tunnel`；X99 当前为 `/home/yuchen/SATA-2/AI/voice-tunnel`）：

```text
<data_dir>/models/       # faster-whisper-*、Qwen3-ASR-0.6B
<data_dir>/venvs/qwen/       # transformers 后端环境
<data_dir>/venvs/qwen-vllm/  # vLLM 流式后端环境
<data_dir>/logs/         # qwen-service.log
```

界面里的“安装位置”可直接改；服务启动时按该目录找环境和权重，找不到才回退到仓库内 `.venv-qwen`。

### Qwen3-ASR 后端（准确率更高）

```bash
DATA_DIR=/home/yuchen/SATA-2/AI/voice-tunnel
# transformers 后端
$DATA_DIR/venvs/qwen/bin/python scripts/qwen_asr_service.py \
  --model $DATA_DIR/models/Qwen3-ASR-0.6B --port 8770
# vLLM 流式后端（端口 8771；0.6B 显存占比 0.35 够用，1.7B 需要 0.75，自动拉起时取 config.ini 的 qwen_gpu_memory_utilization）
$DATA_DIR/venvs/qwen-vllm/bin/python scripts/qwen_asr_service.py \
  --model $DATA_DIR/models/Qwen3-ASR-0.6B --port 8771 --backend vllm --gpu-memory-utilization 0.35
```

在界面“识别后端”选 `Qwen3-ASR` 或 `Qwen3-ASR (vLLM 流式)` 即可；监听进程发现服务未启动会自动拉起（模型常驻，冷启动约 9 秒，之后每次转写不再加载模型）。X99 RTX 2060 实测：3.8 秒中文音频约 1.0 秒出结果，`lang=zh` 映射到 `Chinese`，长音频由模型内部按窗口切块。

重装环境（换 `data_dir` 时）：

```bash
DATA_DIR=/home/yuchen/SATA-2/AI/voice-tunnel
# vLLM 流式环境（约 9.6GB）
uv venv --python 3.10 "$DATA_DIR/venvs/qwen-vllm"
uv pip install --python "$DATA_DIR/venvs/qwen-vllm/bin/python" vllm==0.14.0
uv pip install --python "$DATA_DIR/venvs/qwen-vllm/bin/python" qwen-asr==0.0.6 --no-deps \
  accelerate==1.12.0 nagisa soynlp qwen-omni-utils librosa soundfile sox pytz flask ninja six dyNET38 audioread
```

对比 whisper-small（同一段 TTS 音频）：

| 后端 | 结果 | 耗时(3.8s 音频) |
| --- | --- | --- |
| faster-whisper small | 你好,这是一段幽灵美音在生产的测试音。 | 0.16s |
| Qwen3-ASR-0.6B (transformers) | 你好，这是一段由灵美音色生成的测试语音。 | ~1.0s |
| Qwen3-ASR-0.6B (vLLM) | 同上 | ~0.12s（稳态） |

vLLM 流式实测（同一段音频，1s chunk）：0.5s 出“你好。”→ 1.5s 出“你好，这是一段有林。”→ 2.5s 修正为“你好，这是一段由零点音色生成的测试。”→ 松手收尾为“你好，这是一段由灵美音色生成的测试语音。”。HUD 每秒刷新，松手后再做一次整段微调。

## 桌面界面

![Voice Tunnel GUI](docs/images/gui-preview.png)

```bash
python3 gui/voice_tunnel_gui.py
```

- 顶部一行显示监听状态和当前目标主机；右侧“主机”列表显示各 Agent 在线情况与当前目标
- 可改说话键（点击后按实际按键自动识别）、麦克风、识别后端、模型、GPU/CPU、目标主机、数据目录；流式后端的刷新节拍固定约 0.2s，间隔设置只对整段重解码后端生效
- “保存并应用”会重启监听；PTT 设备是键盘的 Linux event 节点，默认已指向 ROYUAN 键盘，无需手动改
- `Ctrl+R` 开始监听，`Ctrl+T` 停止
- 关闭窗口会最小化到右上角托盘；点击托盘图标在“显示/隐藏主窗口”之间切换，菜单也会同步
- 已安装应用菜单项：`~/.local/share/applications/voice-tunnel.desktop`

## 当前限制

- Linux 文本注入依赖 `xdotool`/`xclip`（剪贴板模式，0.8s 后后台还原文本剪贴板）。
- Windows Agent 必须在用户桌面会话运行（计划任务 VoiceTunnelAgent）；从 SSH 启动会落在 Session 0，Agent 会直接拒绝启动。UAC 高权限窗口不支持。
- 浮窗定位：Ubuntu 跟随鼠标；Windows 取 Win32 caret 或 MSAA caret（Chrome/Electron），拿不到时（如 Windows Terminal）回退到聚焦窗口内的鼠标或窗口左下角。
- 鼠标停在没有 Agent 的屏幕（如 xiaoxin15）时目标为“无”，语音不会再被送到 X99。

## 文档

- `docs/architecture.md`：整体架构和目标跟随设计
- `docs/protocol.md`：TCP/JSON 消息协议
- `docs/implementation-status.md`：已完成 / 下一步
