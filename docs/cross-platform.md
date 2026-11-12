# 跨平台部署方案（Windows / Ubuntu）

> 状态：方案已审核，尚未实施（2026-11-12）

## 目标

服务端、语音主机（监听端 + 识别服务）、Agent 三个角色都能部署在 Windows 或 Ubuntu 上，任意组合。例如：Windows 当语音主机给 Ubuntu 打字，或者全部跑在 Windows 上。

不在本次范围内：macOS；语音主机远程调用另一台机器的显卡做识别（识别服务目前没有认证，开放到局域网前需要先加 token 校验，单独做）；从 Windows 自动拉起 WSL2 里的识别服务。

## 现状（实测）

在 OMEN（Windows 10，Python 3.14）上用当前代码实测：

| 检查 | 结果 |
| --- | --- |
| 全部单元测试 | 37 项中 36 项通过；唯一失败是 `listen.py` 顶层 `import fcntl` 导致监听端模块无法导入 |
| 服务端 `serve` | 正常监听、响应 `status`（只用 `asyncio.run`，无信号处理） |
| 服务端 + dry-run Agent 端到端 | 连接、`send`、ACK 全部正常 |
| 带 BOM 的 `config.ini` | **失败**：`MissingSectionHeaderError`（PowerShell 5 的 `Set-Content -Encoding UTF8`、旧版记事本都会写 BOM） |

各角色的平台支持：

| 角色 | Ubuntu | Windows | 阻塞点 |
| --- | --- | --- | --- |
| Agent | ✅ | ✅ | — |
| 服务端 | ✅ | ✅ | BOM 配置 |
| 监听端 | ✅ | ❌ | `fcntl` 单实例锁；说话键只有 evdev；识别服务自动拉起写死 `bin/python` |
| 识别服务（vLLM 流式） | ✅ | ⚠️ 仅 WSL2 | vLLM 不支持原生 Windows |
| 识别服务（transformers / faster-whisper） | ✅ | ✅（待验证） | 见风险 2 |
| 桌面界面 | ✅ | ❌ | 写死 `.venv/bin/voice-tunnel`；按键捕获依赖 evdev；子进程输出按 UTF-8 解码 |

## 审核结论：对初版方案的修正

1. **说话键改用 `GetAsyncKeyState` 轮询，不用低级键盘钩子。** 初版方案是 `WH_KEYBOARD_LL`。钩子回调必须在 `LowLevelHooksTimeout`（约 300ms–1s）内返回，否则 Windows 会**静默摘掉钩子**；Python 回调要抢 GIL，识别和网络线程繁忙时很容易超时，表现为“说话键突然失灵”且没有任何报错。100Hz 轮询 `GetAsyncKeyState(VK)` 不需要消息循环，CPU 开销可忽略。代价是不能吞掉按键（Linux 的 evdev 也不吞，行为一致），以及漏掉短于 10ms 的按键（对按住说话无影响）。
2. **新增：所有配置读取改为 `utf-8-sig`。** 实测发现。共 5 处：`src/voice_tunnel/config.py` 的 `load_server_config` / `load_agent_config` / `load_voice_config`（第 95、110、131 行），`agent/voice_tunnel_agent.py:77`，`gui/voice_tunnel_gui.py:85`。
3. **新增：GUI 启动子进程时设置 `PYTHONUTF8=1`。** GUI 按 UTF-8 解码监听端输出，而 Windows 子进程默认按本地代码页（GBK）输出，中文日志会乱码。`keytest` 子进程同理。
4. **补充：Windows 上拉起识别服务要改进程创建参数。** `start_new_session` 只在 POSIX 生效。Windows 上需要 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`，否则会弹出控制台窗口，并可能随父进程的控制台一起退出。同时：
   - Python 路径按系统解析（`bin/python` 或 `Scripts\python.exe`）；
   - Windows 上选了 vLLM 后端时不尝试拉起：服务已健康（例如在 WSL2 里手动启动，Windows 访问 `localhost` 会转发进 WSL2）就直接用，否则给出明确提示。
5. **补充：监听端在 Windows 上必须运行在用户桌面会话。** 与 Agent 相同：Session 0 里 `GetAsyncKeyState` 拿不到键盘状态，麦克风也受隐私设置限制。启动时检测到 Session 0 就拒绝启动（复用 Agent 的检测）。
6. **新增：GitHub Actions 双平台单元测试**（ubuntu-latest / windows-latest，不需要 GPU）。仓库已推到 GitHub，成本很低，可以防止以后又引入 Linux 专有代码。

## 实施步骤

| 优先级 | 内容 | 验收 |
| --- | --- | --- |
| P0 | 监听端单实例锁跨平台（Windows 用 `msvcrt.locking`，与 Agent 一致） | Windows 上 `import voice_tunnel.listen` 成功，37 项测试全过 |
| P0 | 配置读取统一 `utf-8-sig` | 新增测试：带 BOM 的配置能正常加载 |
| P0 | `pyproject` 中 `evdev` 加 `sys_platform == "linux"` 标记 | Windows 上 `pip install -e ".[voice]"` 成功 |
| P1 | 说话键后端拆分：Linux evdev / Windows `GetAsyncKeyState`；按键名映射（`KEY_RIGHTCTRL`↔`VK_RCONTROL`、`KEY_PAUSE`、`KEY_F13`… 等常用键），`keytest` 两边可用 | 映射单元测试；Windows 桌面会话下 `keytest` 能识别右 Ctrl |
| P1 | 识别服务拉起：Python 路径、Windows 进程创建参数、vLLM 守卫 | 单元测试覆盖三个分支 |
| P1 | GUI：命令行路径按系统解析，子进程设 `PYTHONUTF8=1` | Windows 上 GUI 能启动监听、日志中文正常 |
| P1 | 监听端 Session 0 检测 | SSH 下启动给出明确拒绝信息 |
| P2 | GitHub Actions 双平台测试 | 两个平台均通过 |
| P2 | 文档：README 平台矩阵、Windows 语音主机部署、WSL2 跑 vLLM、Windows 防火墙放行 8765 | — |

全程保持 Ubuntu 行为不变：现有测试全过，X99 实际使用无回归。

## 风险与待验证项

1. **需要用户在 Windows 桌面会话里验证。** SSH 会话落在 Session 0，收不到键盘输入，也拿不到麦克风，所以说话键与录音无法远程测试，需要用户在 OMEN 桌面上按右 Ctrl 实测一次。
2. **原生 Windows 上的 Qwen3-ASR transformers 后端未验证。** 现有安装命令里 `nagisa`、`dyNET38` 等依赖在 Windows 上是否有 wheel 未知；faster-whisper（CTranslate2 有 Windows CUDA wheel）作为保底一定可用。
3. **前台是管理员权限窗口时，`GetAsyncKeyState` 能否读到按键待验证。** Agent 本来也不支持向 UAC 高权限窗口注入，所以影响有限。
4. **WSL2 下 vLLM 的显存与延迟未测。** WSL2 的 CUDA 有额外开销，冷启动和流式延迟需要实测后更新文档。
5. **Agent 单实例锁是用户级全局锁。** 同一台机器上要跑第二个测试 Agent，需设置 `VOICE_TUNNEL_AGENT_LOCK` 指向别的路径（本次实测即如此），记录在开发文档里即可，不需要改动。
