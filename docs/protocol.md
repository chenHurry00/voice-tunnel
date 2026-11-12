# 协议 v1

传输：TCP，UTF-8，按行分隔的 JSON（每条消息一行，`\n` 结尾）。单行上限 1 MiB。

## 认证

第一条消息必须是 `hello`，token 不匹配则断开。

```json
{"type":"hello","version":1,"role":"agent","host":"yuchen-X99","token":"change-me"}
```

服务端回复：

```json
{"type":"hello_ack","ok":true}
```

## 文本插入

服务端 → Agent：

```json
{"type":"insert_text","request_id":"uuid","text":"检查 PPO 的输入维度","created_at":1794389000.0}
```

Agent → 服务端：

```json
{"type":"ack","request_id":"uuid","ok":true,"error":null}
```

Agent 按 `request_id` 去重；重复请求直接返回 ACK，不重复插入。

## 活跃目标

Agent 收到本机 Deskflow 事件后：

```json
{"type":"active","host":"yuchen-X670","state":"enter","ts":1794389000.0}
```

所有 Agent 每 3 秒（无论是否活跃），服务端回 `pong`：

```json
{"type":"heartbeat","host":"yuchen-X670","active":true,"ts":1794389005.0}
{"type":"pong","ts":1794389005.0}
```

服务端 15 秒收不到某 Agent 的任何消息即断开该连接；Agent 15 秒收不到任何消息即重连。

另一台机器 enter 后，服务端通知其余自认为活跃的 Agent 失活（修复丢失的 leave）：

```json
{"type":"active_state","active":false}
```

## 浮窗（HUD）

服务端 → Agent：

```json
{"type":"hud","event":"listen","level":0.0}
{"type":"hud","event":"partial","text":"检查 PPO 的","stable":3,"level":0.42}
{"type":"hud","event":"level","level":0.18}
{"type":"hud","event":"finish"}
{"type":"hud","event":"commit"}
{"type":"hud","event":"error","text":"未能输入：..."}
{"type":"hud","event":"hide"}
```

`stable` 为已稳定的字数（之后的尾部浅色显示）。Agent 收到 `insert_text` 时自行收起浮窗（等价于 `commit`），注入失败显示 `error`。

## 控制命令（CLI / 监听端 → 服务端）

```json
{"type":"control","command":"resolve"}
{"type":"control","command":"hud","target":"OMEN","event":"partial","text":"...","stable":3,"level":0.4}
{"type":"control","command":"send","target":"OMEN","text":"..."}
{"type":"control","command":"status"}
```

`resolve` 返回当前目标（可能为 `null`）；监听端在按下说话键时调用一次，之后整句都用显式 `target`。

## 本地事件入口（Agent）

```text
POST http://127.0.0.1:8766/active?state=enter
POST http://127.0.0.1:8766/active?state=leave
GET  http://127.0.0.1:8766/health
```

只绑定 loopback。
