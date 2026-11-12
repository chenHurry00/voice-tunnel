#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
TMP="$(mktemp -d)"
SERVER_PID=""
AGENT_PID=""

cleanup() {
  [ -n "$AGENT_PID" ] && kill "$AGENT_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

cat > "$TMP/config.ini" <<'EOF'
[server]
bind = 127.0.0.1
port = 18765
token = smoke-token
default_target = yuchen-X99

[agent]
server = 127.0.0.1
port = 18765
token = smoke-token
host = yuchen-X99
local_port = 18766
hud = false
allow_clipboard_fallback = false
EOF

PYTHONPATH=src python3 -m voice_tunnel.cli --config "$TMP/config.ini" serve >"$TMP/server.log" 2>&1 &
SERVER_PID=$!
VOICE_TUNNEL_AGENT_LOCK="$TMP/agent.lock" python3 agent/voice_tunnel_agent.py --config "$TMP/config.ini" --no-hud --dry-run >"$TMP/agent.log" 2>&1 &
AGENT_PID=$!
sleep 2

PYTHONPATH=src python3 -m voice_tunnel.cli --config "$TMP/config.ini" send \
  --target yuchen-X99 --text "smoke 中文 123"
PYTHONPATH=src python3 -m voice_tunnel.cli --config "$TMP/config.ini" preview \
  --target yuchen-X99 --text "预览 hud"
PYTHONPATH=src python3 -m voice_tunnel.cli --config "$TMP/config.ini" status

grep -q "dry-run inject" "$TMP/agent.log"
echo "smoke test OK"
