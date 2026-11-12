# 实施状态

更新：2026-11-12

## 已完成

- [x] 仓库骨架、README、架构/协议文档
- [x] 协议 v1 设计
- [x] 服务端 TCP/JSON、hello 认证、Agent 注册
- [x] 文本发送 CLI、targets/status
- [x] 单文件 Agent：TCP 客户端、本地 8766 事件入口、心跳、request_id 去重
- [x] Windows SendInput Unicode 注入
- [x] Linux xdotool 注入（无 xdotool 时返回明确错误）
- [x] 无边框紧凑 HUD（半透明、X11 焦点保持）
- [x] dry-run 联调
- [x] X99 + gedit 实测：中英文/符号注入、焦点保持
- [x] 单元测试 + scripts/smoke_test.sh
- [x] x670 / .98 部署并验证 X99→客户端注入（clipboard 模式，文本精确 + 旧剪贴板还原）
- [x] 终端自动使用 Ctrl+Shift+V（xprop WM_CLASS，X99 gnome-terminal 已验证）

## 2026-11-12 修复与重做

- [x] 服务端：Agent 重连时旧连接收尾误删新连接（导致"agent not connected"）
- [x] 服务端：所有 Agent 3s 心跳 + pong，15s 无消息断开；Agent 读超时重连（死连接不再假在线）
- [x] 目标状态机：enter 让其他 Agent 失活、leave 1.5s 宽限、出现过切屏事件后目标未知不再回退 X99
- [x] 监听端：按下即锁定目标，整句浮窗与插入发往同一台机器；浮窗/插入分两条长连接
- [x] 流式：服务端自调度增量解码（新音频合并解一次）、0.12s 推送、静音不解码、停顿 0.7s 整段校正、启动预热；实测首字 0.52s、刷新中位 0.24s、松手到上屏 0.27s
- [x] 浮窗重做：聆听/识别/收尾/错误四态、逐字显现、音量条、提交即消失；Linux fcitx 光标定位、Windows Win32/MSAA 光标定位；GTK override-redirect 弹层 / Tk 不激活显示 + 系统投影 + DPI 感知
- [x] Windows：SendInput 整段一次提交；Session 0 启动直接拒绝；`restart.ps1` 改存 UTF-8 BOM（原文件被 PowerShell 5 按 GBK 解析导致计划任务启动失败）
- [x] Linux 剪贴板还原改为后台进行，ACK 不再等待 1s
- [x] GUI 改版：状态行 + 主机列表 + 克制配色；状态轮询改走控制长连接
- [x] 单元测试：服务端目标状态机、HUD 状态机/定位、fcitx 解析（20 项）
- [x] Ubuntu 浮窗改为跟随鼠标（fcitx 被动上报 / AT-SPI 在终端、微信等程序上不可靠），移除 Linux 光标探测代码；Windows 保持输入光标定位
- [x] 长句：`max_seconds` 30→300（仅防卡键）；服务端 20s 分段定稿、`max_model_len` 4096 / `max_new_tokens` 512；实测 57.6s 连续语音 242 字完整上屏
- [x] GUI 将监听输出同步写入 `<data_dir>/logs/listen.log`
- [x] 审查修复：控制长连接超时不重发（防重复插入）；停顿定稿后静音松手不再解码（防末尾冒出“嗯。”）；HUD 忽略未知事件、Tk 轮询异常后仍续约；Agent 重连后保留当前目标
- [x] 中英混说：ASR 升级到 Qwen3-ASR-1.7B（`--gpu-memory-utilization 0.75`，0.55 时 KV 缓存为负启动失败）；监听端每次按键自动取服务端在线设备名拼进 `context`（`_asr_context`，服务端 `/transcribe`、`/stream/start` 透传到 `init_streaming_state`）；`voice_tunnel/itn.py` 做通用数字规范化（逐位数字串、小数/IP/版本号、带百千万亿的数、百分之、紧邻英文的数、端口/版本等提示词后的数），共 8 项测试
- 修复上下文复述：静音/底噪/开口前，模型会把 `context` 原样当识别结果吐出（浮窗一开始显示提示词），且会被当作前缀污染后续解码；服务端 `echoes_context` 统一拦截（流式解码、停顿/松手 refresh、整段识别），命中即清空结果和前缀；测试覆盖 3 项
- 修复自动拉起（`ensure_qwen_service`）：权重检查支持分片（`model.safetensors.index.json`），1.7B 不再被悄悄换回 0.6B，GUI 模型列表也能列出 1.7B；vLLM 显存占比改为 config 的 `qwen_gpu_memory_utilization`（默认 0.75）；实测停掉服务后由监听端拉起，加载 1.7B 约 60s；5 项测试\n- 16 句真实口述（主机名、端口、IP、型号、术语）对比：0.6B 无上下文 → 1.7B + 通用描述 + 自动设备名 + ITN，设备名、IP、端口、数字类错误基本消除；仍错的是上下文里没有的生僻词（vLLM 大小写、KV cache、Deskflow、Qwen3 念成“千问三”），需要后续的 LLM 整句改写

## 下一步

- [ ] 跨平台部署（语音主机 / 服务端支持 Windows）：方案与实测见 [cross-platform.md](cross-platform.md)
- [ ] 实机确认 Windows 浮窗外观与光标定位（VS Code / Chrome / 记事本 / Windows Terminal）
- [ ] Linux 三台 systemd 自启动；X99 serve/asr/agent 目前在 tmux（vt-serve / vt-asr / vt-agent）

- [x] Windows(.13) Agent 已部署并连接（SendInput 待真实应用复验）
- [ ] Linux 注入测试矩阵（VS Code / 浏览器）——gedit 和 gnome-terminal 已验证
- [ ] AT-SPI（Linux）和 UI Automation / GetCaretPos（Windows）精确定位光标
- [ ] HUD 圆角/阴影等视觉打磨（当前为无边框紧凑矩形）
- [ ] Deskflow enterCommand/exitCommand 事件桥接
- [x] 键盘 PTT（evdev，默认 RightCtrl，udev uaccess ACL）
- [x] USB 麦克风采集（48k → 16k，峰值归一化）
- [x] faster-whisper small/tiny 本地模型 + `voice-tunnel listen`
- [x] 松开后整段识别并自动插入 active target
- [x] partial 伪流式（GPU small 重解码 + 稳定前缀 + 能量门限 + 重复过滤）→ 目标机 HUD
- [x] 修复 partial 48k 音频未重采样导致 3 倍速、乱识别的问题
- [x] 识别结果 zhconv 繁转简
- [x] HUD 长文本可查看（`max_chars=4000`，滚轮回看，无可见滚动条）
- [x] 托盘图标点击切换显示/隐藏，菜单文案同步
- [x] GUI PTT 按键捕获、麦克风/模型/设备下拉、“保存并应用”重启监听
- [x] PyQt5 桌面 GUI：设置、开始/停止、最小化托盘、右上角图标
- [x] X99/x670/.98/Windows(.13) 配置 Deskflow enter/exit 事件命令
- [ ] 真实切屏验证 active_target 自动跟随（Windows/curl 模拟已验证，待实际切屏复验）
- [x] Qwen3-ASR 后端：独立环境（transformers 4.57.6）、`scripts/qwen_asr_service.py`、GUI 后端下拉
- [x] Qwen3-ASR 服务自动拉起（`ensure_qwen_service`，冷启动约 9s，常驻）
- [x] Qwen3-ASR partial 窗口限制（最近 8s，避免长句重解码延迟线性增长）
- [x] 语言代码映射（`zh` → `Chinese`，兼容 auto/英语等）
- [x] vLLM 0.14 流式后端（RTX 2060/sm75，fp16，`/stream/start|chunk|cancel`）
- [x] 流式 partial：1s chunk、`unfixed_token_num=5` 回滚，边说边改前文
- [x] 停顿微调：静音约 1.5s 自动触发 `/stream/refresh`（无前缀整段重解码），前文随时修正
- [x] 松手收尾也用 refresh（官方 finish 的固定前缀会造成复读）
- [x] 修复 refresh 丢失不足 1 chunk 的尾部音频（最后几个字不识别）
- [x] 流式改用 raw_snapshot（未裁剪/未归一化的连续原始音频）——此前 snapshot 的尾部静音裁剪会让 delta 索引错位、推送中断
- [x] 安装位置可配置（`data_dir`，GUI 可改；模型/环境/日志均在该目录下）
- [x] HUD 重做为 GTK3 输入法风格浮窗：圆角卡片、柔和阴影、说话指示点、深色/灰色两段文字
- [x] 注入成功立即收起候选框；6 秒无更新自动消失
- [x] x670/.98 Agent 带 DISPLAY/XAUTHORITY 重启（run-agent.sh），HUD/注入恢复
- [x] Windows core 重启加载事件命令（GUI 不会自动拉起被杀的 core，必须连 GUI 一起重启）
- [x] Agent 单实例锁改为文件锁（msvcrt/flocking，进程死亡自动释放；端口锁受 WSL/Hyper-V 动态端口范围干扰）
- [x] 启动看门狗：25s 内未完成首次连接则进程自退（释放锁）；HUD 初始化挪到网络连接之后，Tk/GTK 卡死只损失 HUD 不影响注入
- [x] Windows 端 Agent 隐藏窗口运行，日志落盘 agent.log/agent.err.log；注意锁屏/深夜会话下 Tk 可能初始化失败，解锁后跑 start-agent.cmd 即可恢复
- [x] 修复 libatspi segfault：去掉 pyatspi 光标探测（与 GTK atk-bridge 冲突，会把 Agent 整个崩掉），设 NO_AT_BRIDGE=1；光标跟随回退指针位置
- [ ] 术语修正
- [ ] 真实语音复测：简体、流式修正、长句 HUD、托盘切换
- [x] Windows 计划任务 VoiceTunnelAgent（ONLOGON，start-agent.cmd：重启 Deskflow + 启动 Agent）
- [ ] systemd（Linux 三台）自启动

## 已知限制

- Linux 注入依赖外部 `xdotool`/`xclip`（X99、x670、.98 均已安装）。
- HUD 优先尝试 AT-SPI 光标，失败回退鼠标；x670 已补 `python3-tk`，.98 已有。
- `active_target` 状态机已实现；四台机器的 Deskflow enter/exit 事件均已配置。Windows 侧 Agent 通过计划任务 VoiceTunnelAgent（登录自启，start-agent.cmd 同时重启 Deskflow）。
- 剪贴板回退只备份/还原文本，图片/HTML 多格式内容不保证还原；Linux 三台均用 `inject_mode=clipboard` 绕开 fcitx/IME 对 xdotool 的转换。
- Windows SendInput 已实现，需在 VS Code/Notepad 手动复验；UAC 高权限窗口与独占全屏游戏（Raw Input/反作弊）不支持，游戏需切无边框窗口；Deskflow 客户端建议以管理员运行。
- 语音识别默认 GPU（RTX 2060，float16）：whisper small 2 秒音频稳态约 0.2 秒；训练占卡时可能变慢，可在 GUI 切回 CPU int8。
- Qwen3-ASR-0.6B 准确率明显高于 whisper small，但 RTF≈0.25（3.8s→1.0s、38s→8.5s，HF transformers 后端）；partial 只解码最近 8 秒，final 仍是整段一次解码。
- Qwen3-ASR 原生 token 流式只在 vLLM 后端提供（`init_streaming_state/streaming_transcribe`），transformers 后端只能整段解码。
- vLLM 0.14 在 RTX 2060（sm75）可跑：fp16、`gpu_memory_utilization=0.55`（约 6.6GB 常驻）、`max_model_len=2048`；FA2 不可用，encoder 自动退到 TORCH_SDPA。
- vLLM 冷启动首次约 55s（含 torch.compile），有编译缓存后约 20–30s；服务常驻后 3.8s 音频转写约 0.12s（transformers 约 1.0s）。
- 流式 chunk 1s：HUD 每秒更新一次，未固定 token 回滚 5 个（`unfixed_token_num=5`）；松手时 `finish_streaming_transcribe` 做整段收尾。
- whisper / Qwen(transformers) 的 partial 是滑窗重解码 + 稳定前缀，不是模型原生 token 流；只有 vLLM 后端是模型内建流式。
- PTT 默认 RightCtrl；如果与 Ctrl 快捷键冲突，用 `voice-tunnel keytest` 换成 Pause/F13。
