#!/bin/bash
AGENT_ID="$1"
CONTENT="$AGENT_CONTENT"
WORKSPACE="$AGENT_WORKSPACE"

HEARTBEAT_FILE="/tmp/agent-heartbeat-${AGENT_ID}"

# Debug mode: set DEBUG=1 env var to enable verbose logging
if [[ "${DEBUG:-}" == "1" ]]; then
  set -x
  DEBUG_LOG="/tmp/spawn-debug-${AGENT_ID}.log"
  exec 2>"$DEBUG_LOG"
fi

echo "Agent $AGENT_ID spawned in $WORKSPACE (DEBUG=${DEBUG:-0})"
cd "$WORKSPACE" || exit 1

# Fix permissions so this agent can write everywhere in the workspace
sudo chmod -R a+rwX . 2>/dev/null

# Random sleep 0-3000ms to avoid IO conflicts when multiple agents spawn concurrently
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
if [ -f /home/agent/.claude.json ]; then
  jq '.teammateMode = "tmux"' /home/agent/.claude.json > /home/agent/.claude.json.tmp && mv /home/agent/.claude.json.tmp /home/agent/.claude.json
else
  echo '{"teammateMode":"tmux"}' > /home/agent/.claude.json
fi

# Ensure workspace is a git repo to bypass trust prompt
if [ ! -d ".git" ]; then
  git init
fi

# --- Workspace isolation via mount namespace ---
# Save prompt to file to avoid quoting issues across namespace boundary
PROMPT_FILE="$WORKSPACE/.agent-prompt-$$"
printf '%s' "$CONTENT" > "$PROMPT_FILE"

# Stash workspace via bind mount before overlaying /workspaces with tmpfs
STASH=$(mktemp -d /tmp/ws-stash-XXXXXX)
sudo mount --bind "$WORKSPACE" "$STASH"

# Write runner script (executed inside the namespace as agent user)
RUNNER=$(mktemp /tmp/agent-run-XXXXXX.sh)
cat > "$RUNNER" <<RUNNER_EOF
#!/bin/bash
cd "$WORKSPACE" || exit 1
CONTENT=\$(cat "$PROMPT_FILE")
rm -f "$PROMPT_FILE"
rm -f "$RUNNER"
exec codex resume --dangerously-bypass-approvals-and-sandbox --model gpt-5.2 "$AGENT_ID" "\$CONTENT"
RUNNER_EOF
chmod 700 "$RUNNER"
sudo chown agent:agent "$RUNNER" 2>/dev/null || true

# Heartbeat loop: touch file every 30s while the agent is running
echo "[DEBUG] Agent $AGENT_ID heartbeat loop starting..." >&2
touch "$HEARTBEAT_FILE"
(while true; do touch "$HEARTBEAT_FILE"; sleep 30; done) &
HEARTBEAT_PID=$!

# Enter mount namespace: hide /workspaces and /app, expose only this agent's workspace.
# Run in the foreground so Codex keeps its TTY (Codex errors if stdin isn't a terminal).
echo "[DEBUG] Creating mount namespace for agent $AGENT_ID..." >&2
sudo -E unshare --mount --propagation unchanged -- bash -c '
  mount -t tmpfs tmpfs /workspaces
  mount -t tmpfs tmpfs /workspace
  mount -t tmpfs tmpfs /app
  chmod 777 /home/agent/.claude/teams 2>/dev/null || true
  TEAMS_DIR="/home/agent/team-history/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "/home/agent/team-history"
  mount --bind "/home/agent/team-history" "/home/agent/team-history"
  mkdir -p "$TEAMS_DIR"
  chmod 777 "$TEAMS_DIR"
  mount --bind "$TEAMS_DIR" /home/agent/.claude/teams
  mkdir -p "'"$WORKSPACE"'"
  mount --bind "'"$STASH"'" "'"$WORKSPACE"'"
  umount -l "'"$STASH"'"
  rmdir "'"$STASH"'"
  exec sudo -E -H -u agent bash "'"$RUNNER"'"
'
RC=$?

kill "$HEARTBEAT_PID" 2>/dev/null || true
rm -f "$HEARTBEAT_FILE"
exit "$RC"
