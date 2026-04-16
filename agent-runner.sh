#!/bin/bash
WORKSPACE="$1"
PROMPT_FILE="$2"
AGENT_ID="$3"

cd "$WORKSPACE" || exit 1
CONTENT=$(cat "$PROMPT_FILE")
rm -f "$PROMPT_FILE"
rm -f "${BASH_SOURCE[0]}"
exec codex resume --dangerously-bypass-approvals-and-sandbox --model gpt-5.2 "$AGENT_ID" "$CONTENT"