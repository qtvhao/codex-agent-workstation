#!/bin/bash

export ANTHROPIC_BASE_URL="https://api.minimax.io/anthropic"
export ANTHROPIC_AUTH_TOKEN="sk-cp-N7jRwy4mVPJ0gALl4X_S2c635vzF9T8cDy-OfIe-30TKt9NoOwCiwvB3-4IQMIpZrjPCZyecB9fFTJiM7fiPBsq6H1HB8YPqoNA5udMAzyFaJHZqxjbkrnU"
export API_TIMEOUT_MS="3000000"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export ANTHROPIC_MODEL="MiniMax-M2.7"
export ANTHROPIC_SMALL_FAST_MODEL="MiniMax-M2.7"
export ANTHROPIC_DEFAULT_SONNET_MODEL="MiniMax-M2.7"
export ANTHROPIC_DEFAULT_OPUS_MODEL="MiniMax-M2.7"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="MiniMax-M2.7"

export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1

nohup bash -c 'cd webui && npm run dev -- --host' > /dev/null 2>&1 &
VITE_PID=$!

claude --dangerously-skip-permissions "$1"

kill $VITE_PID 2>/dev/null
