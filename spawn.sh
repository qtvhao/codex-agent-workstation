#!/bin/bash
AGENT_ID="$1"
CONTENT="${AGENT_CONTENT}"
WORKSPACE="${AGENT_WORKSPACE}"

HEARTBEAT_FILE="/tmp/agent-heartbeat-${AGENT_ID}"

# Debug mode
if [[ "${DEBUG:-}" == "1" ]]; then
  set -x
  DEBUG_LOG="/tmp/spawn-debug-${AGENT_ID}.log"
  exec 2>"$DEBUG_LOG"
fi

echo "Agent $AGENT_ID spawned in $WORKSPACE (DEBUG=${DEBUG:-0})"
cd "$WORKSPACE" || exit 1

# Fix permissions
sudo chmod -R a+rwX . 2>/dev/null

# Random sleep to avoid IO conflicts
sleep "$(awk 'BEGIN{srand(); printf "%.3f", rand()*3}')"

# Ensure agent settings disable extended thinking
mkdir -p .claude
if [ -f .claude/settings.json ]; then
  jq '.alwaysThinkingEnabled = false' .claude/settings.json > .claude/settings.json.tmp && mv .claude/settings.json.tmp .claude/settings.json
else
  echo '{"alwaysThinkingEnabled":false}' > .claude/settings.json
fi

# Ensure agent runs in tmux teammateMode
mkdir -p /home/agent/.claude
mkdir -p /home/agent/.claude/projects
if [ -f /home/agent/.claude.json ]; then
  jq '.teammateMode = "tmux"' /home/agent/.claude.json > /home/agent/.claude.json.tmp && mv /home/agent/.claude.json.tmp /home/agent/.claude.json
else
  echo '{"teammateMode":"tmux"}' > /home/agent/.claude.json
fi

# Ensure workspace is a git repo
if [ ! -d ".git" ]; then
  git init
fi

# Save prompt to file (don't use $$ in filename - it's the parent shell's PID)
# Runner script in /tmp
RUNNER="/tmp/agent-run-${AGENT_ID}.sh"
cat > "$RUNNER" << ENDRUNNER
#!/bin/bash
WORKSPACE="\$1"
AGENT_ID="\$2"
cd "\$WORKSPACE" || exit 1
export HOME="/home/agent"
CONTENT="\${AGENT_CONTENT:-}"
if [ -z "\$CONTENT" ]; then
  echo "ERROR: AGENT_CONTENT is empty" >&2
  exit 1
fi
exec codex --dangerously-bypass-approvals-and-sandbox --model gpt-5.2 "\$CONTENT"
ENDRUNNER
chmod +x "$RUNNER"

# Try mount namespace (best-effort)
NAMESPACE_OK=""
if sudo -E unshare --mount --propagation unchanged -- bash -c "
  mkdir -p /home/agent/.claude/teams 2>/dev/null || true
  TEAMS_DIR=\"/home/agent/team-history/\$(date +%Y%m%d-%H%M%S)\"
  mkdir -p \"\$TEAMS_DIR\" && chmod 777 \"\$TEAMS_DIR\"
  mount --bind \"\$TEAMS_DIR\" /home/agent/.claude/teams 2>/dev/null || true
  exec sudo -E -u agent bash \"$RUNNER\" \"$WORKSPACE\" \"$AGENT_ID\"
" 2>/dev/null; then
  NAMESPACE_OK=1
fi

if [ -z "$NAMESPACE_OK" ]; then
  # Fallback: run directly without namespace isolation
  echo "[DEBUG] Running agent without namespace isolation" >&2
  sudo -E -u agent bash "$RUNNER" "$WORKSPACE" "$AGENT_ID" &
fi

AGENT_PID=$!
echo "[DEBUG] Agent $AGENT_ID heartbeat loop starting (PID=$AGENT_PID)..." >&2

# Heartbeat loop
while kill -0 "$AGENT_PID" 2>/dev/null; do
  touch "$HEARTBEAT_FILE"
  sleep 30
done
echo "[DEBUG] Agent $AGENT_ID process $AGENT_PID died, exit code: $?" >&2
rm -f "$HEARTBEAT_FILE"
wait "$AGENT_PID" 2>/dev/null
