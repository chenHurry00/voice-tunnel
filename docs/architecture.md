# 架构

## 角色

```text
X99 Voice Server
  ├─ 音频采集 / PTT / ASR / 术语修正 / 结果缓存
  ├─ active_target 状态机
  └─ TCP/JSON 文本分发

每台目标主机 Voice Agent
  ├─ 连接 Voice Server，接收 insert_text
  ├─ 本机 127.0.0.1:8766 接收 Deskflow enter/leave
  ├─ 活跃时发送 heartbeat
  ├─ 本机文本注入（Windows SendInput / Linux XTest）
  └─ 光标跟随 HUD（partial / final）
```

Voice Server 与 Deskflow Server 不必是同一台；当前都在 X99。

## 语音链路

```text
PTT evdev 按键按住/松开
  → sounddevice 采集（USB 麦克风，48k 单声道）
  → 线性重采样到 16k + 峰值归一化
  → partial: 预览更新（见下）→ 目标机 HUD
  → 松开: 整段解码（或流式 final）
  → zhconv 繁转简
  → voice-tunnel send（target=active）→ Agent 注入
```

默认不自动按 Enter。partial 只做预览，不写入编辑器；最终文本在松开后插入。

三种识别模式：

| 模式 | partial 做法 | 松手后 | 延迟 |
| --- | --- | --- | --- |
| faster-whisper | 每 0.5s 用 small 重解码全段，取连续两次的最长公共前缀 | 整段解码 | ~0.2s |
| Qwen3-ASR（transformers） | 每 0.8s 重解码最近 8s | 整段解码 | ~1s |
| Qwen3-ASR（vLLM 流式） | 增量推送音频块，模型内部回滚未固定 token 并重出全文 | `finish_streaming_transcribe` 收尾 | 待实测 |

流式（vLLM）与豆包类产品的共同机制：

1. 音频按固定窗口（1s）切块，边录边送；
2. 每个新块到达后，用 `已固定前缀 + 最近若干块` 重新解码，回滚最后几个 token（`unfixed_token_num`），因此前文会被后续语义修正；
3. 松手时把剩余不足一个窗口的音频送入 `finish_streaming_transcribe`，做一次整段收尾（类似“二次微调”）。

服务端接口：`/stream/start`、`/stream/chunk`（`final=true` 收尾）、`/stream/cancel`。

## 文本分发

- 传输：TCP + 换行 JSON，token 认证。
- `request_id` 由服务端生成，Agent 去重，重试不会重复插入。
- 默认只插入文本，不自动按 Enter。

## active_target 状态机

Deskflow 的 `enterCommand` / `exitCommand` 在**被进入/离开的机器**上执行（Flatpak 沙箱内可直接 `curl`）。事件链：

```text
Deskflow 切屏
  → 目标机 enterCommand
  → curl 'http://127.0.0.1:8766/active?state=enter'
  → 本机 Agent 标记 active，并每 5s 向 Voice Server 发 heartbeat
  → 服务端更新 active_target
```

规则：

- `enter` 设置 `active_target`。
- `leave` 只在 `active_target == 自己` 时清除，避免乱序误清。
- 活跃 Agent 5s 心跳；15s 无心跳 → `unknown`，不猜目标。
- 启动默认 `yuchen-X99`。
- 支持手动覆盖；下一次 Deskflow 事件或超时后失效。

## HUD

- partial 只做预览，**不写入编辑器**；final 在松开后插入。
- 位置优先级：光标 → 焦点窗口 → 鼠标；取不到时静默回退。
- 输入法风格浮窗（GTK3 + cairo）：14px 圆角卡片（97% 不透明）、柔和多层阴影、1px 浅描边、左侧蓝色说话指示点；已固定文字深色、未固定候选灰色。Windows 回退 Tk 版（后续再做圆角）。
- 无滚动条；宽度随内容增长（上限 440px），高度按换行行数增长（上限 6 行），超长文本自动跟随末尾，滚轮回看。
- 文本注入成功时候选框立即消失（输入法习惯）；说话中 6 秒无更新自动消失，避免目标切换/注入失败后滞留。
- partial 通过稳定前缀 / 流式回滚减少“呜呜/不然”这类未稳定幻觉。
- 锚点优先级：AT-SPI 光标（Windows GetGUIThreadInfo / Linux 已禁用）→ 鼠标在聚焦窗口内时用鼠标 → 否则贴聚焦窗口左下角 → 兜底鼠标。
- Agent 必须带图形会话环境运行（DISPLAY/XAUTHORITY），远端机器用 `~/voice-tunnel/run-agent.sh` 启动。
- 不使用角落固定 HUD；当前贴着鼠标/光标出现，最大宽度约 46 个字符。
- Linux 后续实验 fcitx4 `InputContext.UpdatePreedit` / `CommitString` 信号，争取做到输入法内联预编辑；失败则保留 HUD。
- Windows 后续用 UI Automation / GetCaretPos 精确定位。

## 文本注入

| 平台 | 首选 | 回退 |
|---|---|---|
| Windows | `SendInput` + `KEYEVENTF_UNICODE` | 无（剪贴板不作为默认） |
| Linux X11 | `inject_mode=clipboard`：xclip + `xprop WM_CLASS` 判断窗口 → 终端 `Ctrl+Shift+V`，其他 `Ctrl+V` | `xdotool type`（仅无 IME 的 US 布局，兼容性差） |

剪贴板会短暂覆盖系统剪贴板；当前只备份/还原文本，图片/HTML 多格式内容不做无损恢复承诺。Linux 部署默认启用 `inject_mode=clipboard`。

## 安全

- 只监听局域网，防火墙限制端口。
- token 认证；不做远程命令执行，只接受 `insert_text`。
- 文本长度上限；Agent 只绑定 `127.0.0.1:8766` 接收 Deskflow 事件。
