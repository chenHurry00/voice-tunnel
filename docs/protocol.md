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

活跃期间每 5 秒：

```json
{"type":"heartbeat","host":"yuchen-X670","active":true,"ts":1794389005.0}
```

服务端维护 `active_target`；15 秒无心跳视为 unknown。

## 流式预览（预留）

```json
{"type":"partial","request_id":"uuid","text":"检查 PPO 的","stable_len":6}
{"type":"final","request_id":"uuid","text":"检查 PPO 的输入维度"}
```

Agent 收到后更新 HUD；`final` 只做展示，真正插入由 `insert_text` 完成。

## 本地事件入口（Agent）

```text
POST http://127.0.0.1:8766/active?state=enter
POST http://127.0.0.1:8766/active?state=leave
GET  http://127.0.0.1:8766/health
```

只绑定 loopback。
