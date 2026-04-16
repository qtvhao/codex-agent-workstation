# Repository Guidelines

## Project Structure & Module Organization
- `agents.py`: main FastAPI service that exposes agent spawn/stream/file endpoints and queue/reaper logic.
- `spawn.sh` and `agent-runner.sh`: runtime scripts used to start isolated agent processes.
- `entrypoint.sh`: container entrypoint that boots the API server.
- `Dockerfile` and `docker-compose.yml`: container build and local orchestration (`codex-agent`, `novnc`).
- `workspace/`: bind-mounted working directory for spawned agents.
- `codex-agent-home/`: persisted agent home/config state.

Keep feature logic in `agents.py` grouped by concern (API models, queueing, spawn, cleanup). Put shell runtime behavior in scripts, not inline Python.

## Build, Test, and Development Commands
- `colima start <profile>`: start Docker runtime on macOS (example: `colima start fuel2-starter`).
- `docker compose up -d --build`: build and start services.
- `docker compose ps`: check container health and exposed ports.
- `docker logs -f codex-agent`: follow API/spawn logs.
- `curl http://localhost:8000/openapi.json`: verify API is reachable.
- `docker compose down`: stop services.

If you change `Dockerfile` or scripts, always rebuild (`--build`) before validating spawn behavior.

## Coding Style & Naming Conventions
- Python: 4-space indentation, `snake_case` for functions/variables, `PascalCase` for Pydantic models.
- Shell: `bash` with explicit variables, quote expansions, and fail fast where practical.
- Prefer clear logs with stable prefixes (for example `[AgentSpawn]`, `[TeamContext]`).
- Keep paths absolute in API payloads (for example `/workspaces/my-project`).

## Testing Guidelines
- No formal test suite is committed yet.
- Minimum validation for changes:
  1. `docker compose up -d --build`
  2. `curl http://localhost:8000/openapi.json`
  3. `POST /agents/` smoke test and verify `docker logs codex-agent` for spawn/reaper behavior.
- When fixing bugs, include the failing command/log snippet and the successful post-fix result in PR notes.

## Commit & Pull Request Guidelines
- Follow existing history style: short, imperative messages (for example `Fix agent spawn PID tracking`, `Update spawn.sh`, `chore: improve container + spawn isolation`).
- Keep commits focused (one logical change per commit).
- PRs should include:
  - What changed and why
  - How to validate (exact commands)
  - Relevant logs or API responses for behavior changes
  - Linked issue/task when available

## Security & Configuration Tips
- Never commit secrets in `codex-agent-home/` or environment files.
- Use `OPENAI_API_KEY` via environment injection, not hardcoded values.
- Treat `workspace/` as untrusted input; avoid broad host-path mounts beyond what this compose file defines.
