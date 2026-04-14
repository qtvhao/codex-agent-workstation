"""Agent spawning API routes."""

import asyncio
import glob
import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents")

DEFAULT_ADVISOR_PERSONA = """You are a senior technical advisor. An executor agent is consulting you for strategic guidance.

Your role:
- Analyze the full context the executor provides (task, code, errors, tool outputs, current approach).
- Respond with DETAILED, actionable advice: step-by-step plans, specific code changes, architectural decisions, debugging strategies.
- If the executor's approach has flaws, explain exactly what's wrong and provide corrected approach.
- If multiple viable paths exist, compare tradeoffs concretely.
- Be thorough — the executor relies on your detailed guidance to proceed correctly.
- Reference specific files, functions, line numbers, and code snippets when relevant.
- If the context is insufficient, say exactly what additional information is needed."""

CLAUDE_JSON_PATH = os.path.expanduser("~/.claude.json")
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CHROME_SPAWNER_URL = os.environ.get("CHROME_SPAWNER_URL", "http://host.docker.internal:8100")

REAPER_INTERVAL = 15  # seconds between reaper sweeps
MIN_SPAWN_GAP = 20  # minimum seconds between consecutive spawns
SPAWN_JSONL_TIMEOUT = 120  # seconds to wait for first JSONL before declaring startup failure
HEARTBEAT_STALE_TIMEOUT = 300  # seconds without heartbeat before declaring agent hung

# Patterns that indicate an agent is accessing sensitive host paths
_HOME_ACCESS_PATTERNS = re.compile(
    r'/home/agent/\.|'   # dotfiles in home (e.g. /home/agent/.claude, /home/agent/.claude.json)
    r'~/\.'              # tilde shorthand for dotfiles (e.g. ~/.claude)
)

# Prohibited pattern — agent is killed immediately if matched
_PROHIBITED_PATTERNS = re.compile(r'\.claude')


def _check_prohibited_access(entry: dict) -> str | None:
    """Check if a JSONL entry contains tool calls or results accessing .claude paths. Returns violation description or None."""
    texts = _extract_tool_texts(entry)
    for label, text in texts:
        m = _PROHIBITED_PATTERNS.search(text)
        if m:
            return f"{label} accessing {m.group()}"
    return None


def _check_home_access(entry: dict) -> str | None:
    """Check if a JSONL entry contains tool calls or results accessing /home/agent/ or ~/."""
    texts = _extract_tool_texts(entry)
    for label, text in texts:
        m = _HOME_ACCESS_PATTERNS.search(text)
        if m:
            return f"{label} accessing {m.group()}"
    return None


def _extract_tool_texts(entry: dict) -> list[tuple[str, str]]:
    """Extract (label, text) pairs from tool calls and results in a JSONL entry."""
    results = []
    entry_type = entry.get("type")

    if entry_type == "assistant":
        msg = entry.get("message", {})
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                input_str = json.dumps(block.get("input", {}))
                tool_name = block.get("name", "unknown")
                results.append((f"tool_call:{tool_name}", input_str))

    if entry_type == "user":
        result = entry.get("toolUseResult", {})
        stdout = result.get("stdout", "")
        if stdout:
            results.append(("tool_result", stdout))
        msg = entry.get("message", {})
        content = msg.get("content", []) if isinstance(msg.get("content"), list) else []
        for block in content:
            if block.get("type") == "tool_result":
                block_content = block.get("content", "")
                if isinstance(block_content, str):
                    results.append(("tool_result", block_content))

    return results


# Store agent state at spawn time
agent_snapshots: dict[str, dict[str, float]] = {}
agent_workspaces: dict[str, str] = {}
agent_processes: dict[str, subprocess.Popen] = {}
agent_teams_enabled: set[str] = set()
agent_spawn_times: dict[str, float] = {}  # monotonic time of spawn
_last_spawn_time: float = 0.0  # monotonic timestamp of last spawn

# Queue for serializing spawns — avoids blocking the FastAPI thread
_spawn_queue: asyncio.Queue = asyncio.Queue()


async def spawn_queue_worker():
    """Drain _spawn_queue one item at a time, enforcing MIN_SPAWN_GAP between spawns."""
    global _last_spawn_time
    while True:
        req = await _spawn_queue.get()
        now = time.monotonic()
        elapsed = now - _last_spawn_time
        if _last_spawn_time > 0 and elapsed < MIN_SPAWN_GAP:
            wait = MIN_SPAWN_GAP - elapsed
            logger.info("spawn throttle: waiting %.1fs before spawning agent %s", wait, req.agent_id[:8])
            await asyncio.sleep(wait)
        _last_spawn_time = time.monotonic()
        try:
            _do_spawn(req)
        except Exception as e:
            logger.error("spawn_queue_worker: failed to spawn agent %s: %s", req.agent_id[:8], e)


def set_trust(workspace: str):
    try:
        with open(CLAUDE_JSON_PATH, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    projects = data.setdefault("projects", {})
    projects.setdefault(workspace, {})["hasTrustDialogAccepted"] = True

    with open(CLAUDE_JSON_PATH, "w") as f:
        json.dump(data, f, indent=2)


def find_jsonl(agent_id: str) -> str | None:
    pattern = os.path.join(CLAUDE_PROJECTS_DIR, "**", f"{agent_id}.jsonl")
    matches = glob.glob(pattern, recursive=True)
    return matches[0] if matches else None


def snapshot_workspace(workspace: str) -> dict[str, float]:
    """Glob all files in workspace and record their mtimes."""
    snapshot = {}
    for root, _, files in os.walk(workspace):
        for name in files:
            full = os.path.join(root, name)
            try:
                snapshot[full] = os.path.getmtime(full)
            except OSError:
                pass
    return snapshot


UUID_RE = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$")

MAX_PROMPT_LENGTH = 20_000  # ~5k tokens, leaves room for system prompt + tools + conversation


class SpawnRequest(BaseModel):
    agent_id: str
    content: str
    workspace: str = Field(
        description=(
            "Absolute path to the agent's working directory. "
            "Must be under /workspaces/ (e.g. /workspaces/my-project). "
            "Paths under /workspace/ (singular) are rejected."
        ),
    )
    browser_use: bool = Field(
        default=False,
        description=(
            "Spawn a dedicated Chrome instance with CDP for this agent. "
            "When enabled, the spawner allocates a free CDP port, launches Chrome via the Chrome Spawner API, "
            "starts a CDP reverse proxy, and writes .mcp.json + mcp-playwright.json into the workspace "
            "so the agent can control the browser through Playwright MCP. "
            "The response includes a 'chrome' object with cdp_port and proxy_port. "
            "Requires browser_profile_name to be set."
        ),
    )
    browser_profile_name: str | None = Field(
        default=None,
        description=(
            "Name for the Chrome profile. Required when browser_use is true. "
            "Used as the Chrome user-data-dir name and displayed in the Chrome Spawner registry."
        ),
    )
    agent_teams: bool = Field(default=False)
    model: str = Field(default="sonnet")
    effort: str = Field(default="low")

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if not UUID_RE.match(v):
            raise ValueError("agent_id must be a valid UUID")
        return v

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        normalized = os.path.normpath(v)
        if normalized == "/workspace" or normalized.startswith("/workspace/"):
            raise ValueError(
                "workspace must be under /workspaces/, not /workspace/. "
                "Use /workspaces/<name> instead."
            )
        return v

    @field_validator("content")
    @classmethod
    def validate_content_length(cls, v: str) -> str:
        if len(v) > MAX_PROMPT_LENGTH:
            raise ValueError(
                f"prompt too long: {len(v)} chars (max {MAX_PROMPT_LENGTH}). "
                f"Reduce content or split into smaller tasks."
            )
        return v

    @model_validator(mode="after")
    def validate_browser_profile_name(self):
        if self.browser_use and not self.browser_profile_name:
            raise ValueError("browser_profile_name is required when browser_use is true")
        return self


class AdvisorContext(BaseModel):
    """Structured context the executor sends to the advisor."""
    # --- Required fields ---
    task: str = Field(description="The original task or goal the executor is working on.")
    current_approach: str = Field(description="What the executor has done so far and its current strategy.")
    expected_outcome: str = Field(description="What success looks like — the desired end state or acceptance criteria.")
    codebase_summary: str = Field(description="High-level overview of the project: structure, main modules, tech stack.")
    key_files: list[str] = Field(
        description="Relevant file paths and their content summaries or snippets.",
    )
    questions: list[str] = Field(
        description="Specific questions the executor needs the advisor to answer.",
    )
    attempted_solutions: list[str] = Field(
        description="Approaches already tried and why they failed or were abandoned.",
    )
    # --- Optional fields ---
    errors: list[str] = Field(
        default_factory=list,
        description="Errors, failures, or unexpected outputs encountered.",
    )
    tool_outputs: str = Field(
        default="",
        description="Relevant tool call results, test outputs, or command outputs.",
    )
    constraints: str = Field(
        default="",
        description="Known constraints: deadlines, tech stack requirements, performance targets, etc.",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="Relevant packages/libraries and their versions.",
    )
    git_diff: str = Field(
        default="",
        description="Current uncommitted changes or recent diff relevant to the task.",
    )
    environment: str = Field(
        default="",
        description="Runtime environment details: OS, language version, container, CI, etc.",
    )
    relevant_docs: list[str] = Field(
        default_factory=list,
        description="Documentation snippets, links, or references relevant to the task.",
    )

    def render(self) -> str:
        """Render structured context into a readable prompt section."""
        sections = [
            f"## Task\n{self.task}",
            f"## Expected Outcome\n{self.expected_outcome}",
            f"## Codebase Summary\n{self.codebase_summary}",
            f"## Current Approach\n{self.current_approach}",
            "## Attempted Solutions\n" + "\n".join(f"- {s}" for s in self.attempted_solutions),
            "## Key Files\n" + "\n".join(f"- {f}" for f in self.key_files),
        ]
        if self.errors:
            sections.append("## Errors\n" + "\n".join(f"- {e}" for e in self.errors))
        if self.tool_outputs:
            sections.append(f"## Tool Outputs\n{self.tool_outputs}")
        if self.constraints:
            sections.append(f"## Constraints\n{self.constraints}")
        if self.dependencies:
            sections.append("## Dependencies\n" + "\n".join(f"- {d}" for d in self.dependencies))
        if self.git_diff:
            sections.append(f"## Git Diff\n```\n{self.git_diff}\n```")
        if self.environment:
            sections.append(f"## Environment\n{self.environment}")
        if self.relevant_docs:
            sections.append("## Relevant Docs\n" + "\n".join(f"- {d}" for d in self.relevant_docs))
        sections.append("## Questions\n" + "\n".join(f"- {q}" for q in self.questions))
        return "\n\n".join(sections)


class AdvisorSpawnRequest(BaseModel):
    """Request body for /spawn-advisor — mirrors SpawnRequest but wraps structured context with advisor persona."""
    agent_id: str
    context: AdvisorContext
    workspace: str = Field(
        description=(
            "Absolute path to the agent's working directory. "
            "Must be under /workspaces/ (e.g. /workspaces/my-project). "
            "Paths under /workspace/ (singular) are rejected."
        ),
    )
    model: str = "sonnet"
    effort: str = "low"
    detail_level: str = Field(
        default="low",
        description="Level of detail in the advisor response: high, medium, or low.",
    )

    @field_validator("detail_level")
    @classmethod
    def validate_detail_level(cls, v: str) -> str:
        if v not in ("high", "medium", "low"):
            raise ValueError("detail_level must be one of: high, medium, low")
        return v

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if not UUID_RE.match(v):
            raise ValueError("agent_id must be a valid UUID")
        return v

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        normalized = os.path.normpath(v)
        if normalized == "/workspace" or normalized.startswith("/workspace/"):
            raise ValueError(
                "workspace must be under /workspaces/, not /workspace/. "
                "Use /workspaces/<name> instead."
            )
        return v


@router.post("/spawn-advisor", status_code=201)
async def spawn_advisor(req: AdvisorSpawnRequest):
    # Build the full prompt: advisor persona + detail instruction + rendered structured context
    rendered_context = req.context.render()
    detail_instructions = {
        "high": "Respond with maximum detail: full step-by-step plans, complete code snippets, thorough explanations of tradeoffs, and edge cases.",
        "medium": "Respond with moderate detail: clear action plan with key code snippets, brief tradeoff analysis.",
        "low": "Respond concisely: enumerated steps, no lengthy explanations. Under 200 words.",
    }
    detail_line = f"\n\nDetail level: {req.detail_level.upper()}. {detail_instructions[req.detail_level]}"
    output_file = os.path.join(req.workspace, f"ADVISOR_RESPONSE.md")
    output_instruction = (
        f"\n\nIMPORTANT: Write your ENTIRE response to the file `{output_file}`. "
        f"Use the Write tool to create this file with your full advice as markdown. "
        f"This is how the executor will read your response."
    )
    content = f"{DEFAULT_ADVISOR_PERSONA}{detail_line}{output_instruction}\n\n---\n\n# Executor Context\n\n{rendered_context}"

    if len(content) > MAX_PROMPT_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Rendered context too long: {len(content)} chars (max {MAX_PROMPT_LENGTH}). "
                   f"Reduce content or split into smaller tasks.",
        )

    # Reuse the standard SpawnRequest flow
    spawn_req = SpawnRequest(
        agent_id=req.agent_id,
        content=content,
        workspace=req.workspace,
        model=req.model,
        effort=req.effort,
    )

    logger.info("spawn_advisor called: agent_id=%s workspace=%s model=%s context_len=%d",
                req.agent_id, req.workspace, req.model, len(rendered_context))
    await _spawn_queue.put(spawn_req)
    queue_pos = _spawn_queue.qsize()
    logger.info("advisor %s enqueued (queue size now %d)", req.agent_id[:8], queue_pos)
    return JSONResponse(status_code=201, content={"agent_id": req.agent_id, "queue_position": queue_pos})


def _parse_team_context(content: str) -> dict | None:
    """Extract team context from agent prompt content.

    Looks for patterns like:
    - '**Team**: team-name'
    - 'TeamCreate' tool mentions
    - Number of agents/members mentioned
    """
    if not content:
        return None

    result = {}

    # Extract team name from **Team**: pattern
    team_match = re.search(r'\*\*Team\*\*:\s*([^\s\n]+)', content)
    if team_match:
        result['team_name'] = team_match.group(1)

    # Extract team name from team_name= or "team_name": patterns
    team_name_match = re.search(r'["\']team_name["\']:\s*["\']([^"\']+)["\']', content)
    if team_name_match:
        result['team_name'] = team_name_match.group(1)

    # Count Agent tool calls (spawned teammates) - look for 'name': 'agent-name' patterns
    agent_count = len(re.findall(r'["\']name["\']:\s*["\']([a-zA-Z0-9_-]+)["\']', content))
    if agent_count > 0:
        result['member_count'] = agent_count
        logger.debug("[TeamContext] detected %d Agent spawns in prompt", agent_count)

    # Detect TeamCreate calls
    if 'TeamCreate' in content:
        result['team_created'] = True
        logger.debug("[TeamContext] TeamCreate detected in prompt")

    # Detect teammateMode
    if 'teammateMode' in content or 'teammate_mode' in content:
        result['teammate_mode'] = True
        logger.debug("[TeamContext] teammateMode referenced in prompt")

    return result if result else None


def extract_inbox_context(inbox_path: str) -> dict:
    """Extract team/agent context from inbox file path.

    Path format: team-history/{timestamp}/{team-name}/inboxes/{agent-name}.json
    Returns dict with team_name, agent_name, timestamp.
    """
    result = {'team_name': None, 'agent_name': None, 'timestamp': None}
    parts = Path(inbox_path).parts
    for i, part in enumerate(parts):
        if part == 'team-history' and i + 2 < len(parts):
            result['timestamp'] = parts[i + 1]
            result['team_name'] = parts[i + 2]
        if part == 'inboxes' and i + 1 < len(parts):
            result['agent_name'] = parts[i + 1].replace('.json', '')
    return result


def _find_free_cdp_port() -> int:
    """Find a free CDP port by querying existing profiles and picking the next available."""
    try:
        resp = httpx.get(f"{CHROME_SPAWNER_URL}/chrome", timeout=10)
        resp.raise_for_status()
        used_ports = {info["cdp_port"] for info in resp.json().values()}
    except Exception:
        used_ports = set()
    # Search in range 9222-9399 for a free port
    for port in range(9222, 9400):
        if port not in used_ports:
            return port
    raise RuntimeError("No free CDP port available in range 9222-9399")


def _resolve_host_ip() -> str:
    """Resolve host.docker.internal to an IP address."""
    import socket as _socket
    try:
        return _socket.gethostbyname("host.docker.internal")
    except _socket.gaierror:
        return "host.docker.internal"


def _wait_for_cdp(cdp_port: int, timeout: int = 30):
    """Poll CDP endpoint until it responds or timeout is reached."""
    host_ip = _resolve_host_ip()
    url = f"http://{host_ip}:{cdp_port}/json/version"
    for _ in range(timeout * 2):
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"CDP port {cdp_port} not ready after {timeout}s")


def spawn_chrome_profile(profile_name: str, download_path: str | None = None) -> dict:
    """Spawn a Chrome instance via the Chrome Spawner API. Returns profile info.

    Deletes any existing profile with the same name before spawning,
    then waits for the CDP port to be ready.
    """
    try:
        httpx.delete(f"{CHROME_SPAWNER_URL}/chrome/{profile_name}", timeout=10)
    except Exception:
        pass
    cdp_port = _find_free_cdp_port()
    body = {"profile_name": profile_name, "cdp_port": cdp_port}
    if download_path:
        body["download_path"] = download_path
    resp = httpx.post(
        f"{CHROME_SPAWNER_URL}/chrome",
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    _wait_for_cdp(result["proxy_port"])
    return result


def write_mcp_config(workspace: str, proxy_port: int):
    """Write .mcp.json and mcp-playwright.json into the workspace directory."""
    workspace_name = os.path.basename(os.path.normpath(workspace))
    workspace_container_path = f"/workspaces/{workspace_name}"

    mcp_json = {
        "mcpServers": {
            "playwright": {
                "type": "stdio",
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    "--config",
                    f"{workspace_container_path}/mcp-playwright.json",
                ],
            }
        }
    }

    import socket as _socket
    try:
        host_ip = _socket.gethostbyname("host.docker.internal")
    except _socket.gaierror:
        host_ip = "host.docker.internal"

    playwright_config = {
        "browser": {
            "browserName": "chromium",
            "cdpEndpoint": f"http://{host_ip}:{proxy_port}",
        }
    }

    mcp_path = os.path.join(workspace, ".mcp.json")
    playwright_path = os.path.join(workspace, "mcp-playwright.json")

    if not os.path.exists(mcp_path):
        with open(mcp_path, "w") as f:
            json.dump(mcp_json, f)
    if not os.path.exists(playwright_path):
        with open(playwright_path, "w") as f:
            json.dump(playwright_config, f)

    logger.info("Wrote MCP config to %s (cdp proxy port %d)", workspace, proxy_port)


def _do_spawn(req: SpawnRequest):
    """Execute the actual spawn — called by spawn_queue_worker."""
    os.makedirs(req.workspace, exist_ok=True)
    subprocess.run(["chmod", "-R", "a+rwX", req.workspace],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    set_trust(req.workspace)
    agent_snapshots[req.agent_id] = snapshot_workspace(req.workspace)
    agent_workspaces[req.agent_id] = req.workspace
    short_id = req.agent_id[:8]

    content = req.content
    chrome_info = None

    if req.browser_use:
        try:
            workspace_name = os.path.basename(os.path.normpath(req.workspace))
            download_path = os.path.join("/workspaces", workspace_name, "downloads")
            chrome_info = spawn_chrome_profile(req.browser_profile_name, download_path=download_path)
            write_mcp_config(req.workspace, chrome_info["proxy_port"])
            logger.info("[%s] Chrome spawned: cdp_port=%s proxy_port=%s",
                        short_id, chrome_info["cdp_port"], chrome_info["proxy_port"])
            content += (
                "\n\nYou have a Playwright MCP server configured in .mcp.json. "
                "Use the Playwright MCP tools (browser_navigate, browser_snapshot, browser_click, etc.) "
                "to interact with web pages in the browser."
            )
        except Exception as e:
            raise HTTPException(500, f"Failed to spawn Chrome for agent: {e}")

    env = {**os.environ, "AGENT_CONTENT": content, "AGENT_WORKSPACE": req.workspace, "DEBUG": "1"}
    if req.agent_teams:
        env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
        agent_teams_enabled.add(req.agent_id)
        logger.debug("[AgentSpawn] teammateMode enabled for agent_id=%s workspace=%s", short_id, req.workspace)

    # Parse team context from content if present
    team_context = _parse_team_context(content)
    if team_context:
        logger.debug("[TeamContext] agent_id=%s team=%s members=%d", short_id, team_context.get('team_name', 'unknown'), team_context.get('member_count', 0))
        logger.debug("[TeammateInit] agent_id=%s registering as teammate in team %s", short_id, team_context.get('team_name', 'N/A'))

    proc = subprocess.Popen(
        ["xterm", "-hold", "-title", f"Agent {short_id}",
         "-e", f"/app/spawn.sh {req.agent_id}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    agent_processes[req.agent_id] = proc
    agent_spawn_times[req.agent_id] = time.monotonic()
    debug_log_path = os.path.expanduser(f"~/.claude/debug/{req.agent_id}.txt")
    logger.info("[%s] debug log: %s", short_id, debug_log_path)
    # Log team-aware spawn
    if req.agent_teams or team_context:
        logger.info("[AgentSpawn] teammate agent spawned: id=%s team=%s teammates=%s",
                    short_id,
                    team_context.get('team_name', 'unknown') if team_context else 'N/A',
                    team_context.get('member_count', 0) if team_context else 0)
        logger.debug("[TeammateMailbox] agent_id=%s mailbox initialized", short_id)
    else:
        logger.info("spawn_queue_worker: spawned agent %s", short_id)


@router.post("/", status_code=201)
async def spawn_agent(req: SpawnRequest):
    logger.info("spawn_agent called: agent_id=%s workspace=%s browser_use=%s content_len=%d",
                req.agent_id, req.workspace, req.browser_use, len(req.content))
    await _spawn_queue.put(req)
    queue_pos = _spawn_queue.qsize()
    logger.info("agent %s enqueued (queue size now %d)", req.agent_id[:8], queue_pos)
    return JSONResponse(status_code=201, content={"agent_id": req.agent_id, "queue_position": queue_pos})


@router.get("/{agent_id}/files")
def list_agent_files(agent_id: str):
    if agent_id not in agent_snapshots:
        raise HTTPException(404, f"agent {agent_id} not found")

    before = agent_snapshots[agent_id]
    workspace = agent_workspaces[agent_id]

    created = []
    modified = []
    deleted = []

    # Check current files
    current_files = snapshot_workspace(workspace)

    for path, mtime in current_files.items():
        if path not in before:
            created.append(path)
        elif mtime != before[path]:
            modified.append(path)

    for path in before:
        if path not in current_files:
            deleted.append(path)

    return {
        "agent_id": agent_id,
        "created": sorted(created),
        "modified": sorted(modified),
        "deleted": sorted(deleted),
    }



def _get_pid_cmdline(pid: int) -> str:
    """Read /proc/<pid>/cmdline and return as a space-joined string."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


# Track when subagents were last seen active so we can reset the leader's
# idle age when subagents finish — giving it a fresh window to process results.
_subagent_last_seen: dict[str, float] = {}


def _jsonl_last_entry_is_pending_tool(path: str) -> bool:
    """Check if the last entry in a JSONL is an assistant message with tool_use
    and no corresponding result — meaning a tool is still executing."""
    try:
        # Read last few KB to find the last complete JSON line
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 16384)
            f.seek(size - read_size)
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [l for l in chunk.strip().split("\n") if l.strip()]
        if not lines:
            return False
        import json as _json
        last = _json.loads(lines[-1])
        if last.get("type") == "assistant":
            msg = last.get("message", {})
            if msg.get("stop_reason") == "tool_use":
                return True
    except Exception:
        pass
    return False


def _has_active_subagent_jsonls(agent_id: str, max_stale_seconds: float = 60) -> bool:
    """Check if the agent has subagent JSONL files that are still being written to,
    or if any subagent's last entry is a pending tool call (tool still executing)."""
    pattern = os.path.join(CLAUDE_PROJECTS_DIR, "**", agent_id, "subagents", "*.jsonl")
    matches = glob.glob(pattern, recursive=True)
    if not matches:
        return False
    now = time.time()
    short = agent_id[:8]
    for path in matches:
        try:
            name = os.path.basename(path)
            mtime = os.path.getmtime(path)
            age = now - mtime
            fresh = age <= max_stale_seconds
            pending_tool = False
            if not fresh:
                pending_tool = _jsonl_last_entry_is_pending_tool(path)
            active = fresh or pending_tool
            logger.info(
                "[%s] subagent_jsonl: %s mtime_age=%.0fs fresh=%s pending_tool=%s active=%s",
                short, name, age, fresh, pending_tool, active,
            )
            if active:
                return True
        except OSError:
            pass
    return False


def _log_teammate_events(agent_id: str) -> tuple[list[str], list[str]]:
    """Scan agent's own JSONL for teammate_spawned and teammate_stopped toolUseResult entries.

    Returns (spawned_ids, stopped_ids) lists."""
    import json as _json
    short = agent_id[:8]
    jsonl_path = find_jsonl(agent_id)
    if not jsonl_path:
        logger.info("[%s] _log_teammate_events: no JSONL found for agent_id=%s", short, agent_id)
        return [], []
    spawned, stopped = [], []
    try:
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read_size = min(size, 262144)  # read up to 256KB
            f.seek(max(0, size - read_size))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [l for l in chunk.strip().split("\n") if l.strip()]
        logger.info("[%s] _log_teammate_events: scanning %d lines from %s (size=%d read=%d)",
                     short, len(lines), os.path.basename(jsonl_path), size, read_size)
        # Sample a few lines to understand JSONL structure
        for i, line in enumerate(lines[-5:]):
            try:
                entry = _json.loads(line)
                top_keys = list(entry.keys())
                logger.debug("[%s] _log_teammate_events: line[-%d] top_keys=%s", short, 5 - i, top_keys)
            except (_json.JSONDecodeError, Exception):
                pass
        for line_idx, line in enumerate(lines):
            try:
                entry = _json.loads(line)
                tool_use_result = entry.get("toolUseResult", {})
                if tool_use_result:
                    status = tool_use_result.get("status")
                    logger.debug("[%s] _log_teammate_events: line[%d] has toolUseResult status=%s",
                                 short, line_idx, status)
                status = tool_use_result.get("status")
                if status not in ("teammate_spawned", "teammate_terminated"):
                    continue
                # Extract agent_id from the tool result content
                content = tool_use_result.get("content", [])
                agent_id_val = None
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            text = item["text"]
                            if "agent_id:" in text:
                                for part in text.split():
                                    if part.startswith("agent_id:"):
                                        agent_id_val = part.split("agent_id:", 1)[1]
                                        break
                logger.info(
                    "[%s] teammate_%s: agent_id=%s jsonl=%s line[%d]",
                    short, status, agent_id_val, os.path.basename(jsonl_path), line_idx,
                )
                if agent_id_val:
                    if status == "teammate_spawned":
                        spawned.append(agent_id_val)
                    else:
                        stopped.append(agent_id_val)
            except (_json.JSONDecodeError, Exception):
                continue
        logger.info("[%s] _log_teammate_events: result spawned=%d stopped=%d", short, len(spawned), len(stopped))
    except OSError as e:
        logger.info("[%s] _log_teammate_events: OSError %s", short, e)
    return spawned, stopped


def has_active_subagents(agent_id: str) -> bool:
    """Check if the agent's process tree still has running children,
    including subprocesses like researcher_cli.py in different process groups,
    or background Agent tool sub-agents (detected via subagent JSONL files)."""
    short = agent_id[:8]
    proc = agent_processes.get(agent_id)
    if not proc:
        return False

    # Debug: log teammate_spawned and teammate_stopped entries from agent's JSONL
    spawned, stopped = _log_teammate_events(agent_id)

    # If spawned > stopped, un-stopped teammates are still running
    if len(spawned) > len(stopped):
        logger.info(
            "[%s] has_active_subagents: %d spawned > %d stopped (%d un-stopped teammates)",
            short, len(spawned), len(stopped), len(spawned) - len(stopped),
        )
        return True

    # Check for active subagent JSONL files (background Agent tool sub-agents)
    if _has_active_subagent_jsonls(agent_id):
        logger.info("[%s] has_active_subagents: active subagent JSONLs found", short)
        return True

    try:
        descendants = _get_descendant_pids(proc.pid)
        logger.info("[%s] has_active_subagents: %d descendants (jsonl=inactive)", short, len(descendants))
        for pid in descendants:
            if pid == proc.pid:
                continue
            cmdline = _get_pid_cmdline(pid)
            if cmdline and "researcher_cli" in cmdline:
                logger.info("[%s] has_active_subagents: found researcher_cli pid=%d", short, pid)
                return True
        # Fall back to process group check
        pgid = os.getpgid(proc.pid)
        result = subprocess.run(
            ["pgrep", "-g", str(pgid)],
            capture_output=True, text=True, timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split() if p]
        logger.info("[%s] has_active_subagents: pgid=%d pids=%d", short, pgid, len(pids))
        return len(pids) > 1
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired, ValueError) as e:
        logger.info("[%s] has_active_subagents: exception %s", short, e)
        return False


def _get_descendant_pids(pid: int) -> list[int]:
    """Recursively collect all descendant PIDs via /proc/<pid>/task/<pid>/children."""
    pids = [pid]
    try:
        with open(f"/proc/{pid}/task/{pid}/children", "r") as f:
            for child in f.read().split():
                pids.extend(_get_descendant_pids(int(child)))
    except (OSError, ValueError):
        pass
    return pids


def _get_socket_inodes(pids: list[int]) -> set[int]:
    """Collect socket inodes from /proc/<pid>/fd/ for all given PIDs."""
    inodes = set()
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                    if link.startswith("socket:["):
                        inodes.add(int(link[8:-1]))
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
    return inodes


def _get_established_inodes() -> set[int]:
    """Read /proc/net/tcp and return inodes of ESTABLISHED (state=01) connections."""
    inodes = set()
    for tcp_path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(tcp_path, "r") as f:
                for line in f:
                    fields = line.split()
                    # skip header; st field is index 3, inode is index 9
                    if len(fields) >= 10 and fields[3] == "01":
                        try:
                            inodes.add(int(fields[9]))
                        except ValueError:
                            pass
        except OSError:
            pass
    return inodes


def get_agent_net_connections(agent_id: str) -> int:
    """Count ESTABLISHED TCP connections owned by the agent's process tree."""
    proc = agent_processes.get(agent_id)
    if not proc:
        return 0
    try:
        tree_pids = _get_descendant_pids(proc.pid)
    except Exception:
        return 0
    sock_inodes = _get_socket_inodes(tree_pids)
    if not sock_inodes:
        return 0
    established = _get_established_inodes()
    return len(sock_inodes & established)


def cleanup_agent(agent_id: str):
    proc = agent_processes.pop(agent_id, None)
    if proc:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    agent_snapshots.pop(agent_id, None)
    agent_workspaces.pop(agent_id, None)
    agent_spawn_times.pop(agent_id, None)
    _subagent_last_seen.pop(agent_id, None)
    is_teammate = agent_id in agent_teams_enabled
    agent_teams_enabled.discard(agent_id)
    if is_teammate:
        # Log inbox cleanup for teammate agent
        inbox_path = f"/home/agent/.claude/teams/{agent_id}/inboxes/{agent_id}.json"
        inbox_ctx = extract_inbox_context(inbox_path)
        logger.debug("[TeammateCleanup] teammate agent cleaned up: id=%s team=%s agent=%s",
                    agent_id[:8], inbox_ctx.get('team_name', 'N/A'), inbox_ctx.get('agent_name', 'N/A'))
    try:
        os.remove(f"/tmp/agent-heartbeat-{agent_id}")
    except OSError:
        pass


def parse_timestamp(ts: str) -> float:
    """Parse ISO 8601 timestamp to unix epoch seconds."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.timestamp()


def check_idle_done(agent_id: str, last_entry: dict) -> bool:
    """Check if the agent is done based on idle time.

    - assistant with stop_reason=end_turn: done after 10s idle
    - assistant with stop_reason!=end_turn (null, tool_use): done after 120s idle
    - result type: done immediately
    """
    short = agent_id[:8]
    etype = last_entry.get("type", "")
    if etype == "result":
        logger.info("[%s] idle_check: result entry → done", short)
        return True
    ts = last_entry.get("timestamp")
    if not ts:
        return False
    try:
        age = time.time() - parse_timestamp(ts)
    except (ValueError, OSError):
        return False

    net_conns = get_agent_net_connections(agent_id)
    subagents = has_active_subagents(agent_id)

    # Track subagent activity; when subagents finish, reset age so the
    # leader gets a fresh idle window to process final subagent messages.
    now = time.time()
    if subagents:
        _subagent_last_seen[agent_id] = now
    elif agent_id in _subagent_last_seen:
        age_since_subagent = now - _subagent_last_seen[agent_id]
        if age_since_subagent < age:
            logger.info("[%s] idle_check: resetting age %.0fs → %.0fs (subagents just finished)", short, age, age_since_subagent)
            age = age_since_subagent

    # If spawned > stopped, un-stopped teammates are still running — reset age
    # so the leader gets a fresh idle window to process their results.
    spawned, stopped = _log_teammate_events(agent_id)
    if len(spawned) > len(stopped):
        unstopped = len(spawned) - len(stopped)
        logger.info("[%s] idle_check: resetting age → 0s (%d un-stopped teammates)", short, unstopped)
        age = 0

    if etype == "assistant":
        stop_reason = last_entry.get("message", {}).get("stop_reason")
        logger.info(
            "[%s] idle_check: age=%.0fs stop_reason=%s net_conns=%d subagents=%s",
            short, age, stop_reason, net_conns, subagents,
        )
        if stop_reason == "end_turn":
            if subagents:
                logger.info("[%s] idle_check: end_turn but subagents active — not done (age=%.0fs, net_conns=%d)", short, age, net_conns)
                return False
            is_team = agent_id in agent_teams_enabled
            end_turn_timeout = 120 if is_team else 10
            done = age > end_turn_timeout and net_conns < 2
            logger.info("[%s] idle_check: end_turn age=%.0fs/%ds net_conns=%d teams=%s done=%s", short, age, end_turn_timeout, net_conns, is_team, done)
            return done
        done = age > 120 and not subagents and net_conns < 2
        if done:
            logger.info("[%s] idle_check: → done (stop_reason=%s, age=%.0fs, net_conns=%d, subagents=%s)", short, stop_reason, age, net_conns, subagents)
        elif age > 60:
            logger.debug("[%s] idle_check: not done yet — age=%.0fs/120s net_conns=%d subagents=%s", short, age, net_conns, subagents)
        return done
    return False


def _is_agent_process_alive(agent_id: str) -> bool:
    """Check if the agent's inner process (not just xterm -hold wrapper) is still running."""
    proc = agent_processes.get(agent_id)
    if not proc:
        logger.debug("_is_agent_process_alive(%s): no proc in agent_processes", agent_id[:8])
        return False
    if proc.poll() is not None:
        logger.debug("_is_agent_process_alive(%s): proc.poll() returned %s (dead)", agent_id[:8], proc.poll())
        return False
    # xterm -hold keeps the wrapper alive after the child exits.
    # If only xterm itself remains (1 pid), the inner agent is gone.
    try:
        descendants = _get_descendant_pids(proc.pid)
        alive = len(descendants) > 1
        logger.debug("_is_agent_process_alive(%s): proc.pid=%d, descendants=%s (count=%d), alive=%s",
                     agent_id[:8], proc.pid, descendants, len(descendants), alive)
        return alive
    except Exception as e:
        logger.debug("_is_agent_process_alive(%s): exception getting descendants: %s", agent_id[:8], e)
        return False


def _is_agent_heartbeat_stale(agent_id: str) -> bool:
    """Check if the agent's heartbeat file is stale (not updated recently).

    Returns False if no heartbeat file exists yet (agent still starting up).
    """
    heartbeat_path = f"/tmp/agent-heartbeat-{agent_id}"
    try:
        mtime = os.path.getmtime(heartbeat_path)
        return (time.time() - mtime) > HEARTBEAT_STALE_TIMEOUT
    except OSError:
        return False


def get_last_relevant_entry(path: str) -> dict:
    """Read the JSONL file and return the last assistant/result entry."""
    last_entry = {}
    try:
        with open(path, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("type") in ("assistant", "result") and entry.get("timestamp"):
                        last_entry = entry
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        pass
    return last_entry


async def reaper_loop():
    """Background task that periodically checks all tracked agents for completion."""
    while True:
        await asyncio.sleep(REAPER_INTERVAL)
        for agent_id in list(agent_processes):
            # Liveness check: is the inner agent process still running?
            if not _is_agent_process_alive(agent_id):
                logger.info("reaper: agent %s process tree is dead, cleaning up", agent_id[:8])
                cleanup_agent(agent_id)
                continue
            # Heartbeat stale check: agent process alive but not making progress
            if _is_agent_heartbeat_stale(agent_id):
                logger.error("reaper: agent %s heartbeat stale (>%ds), cleaning up", agent_id[:8], HEARTBEAT_STALE_TIMEOUT)
                cleanup_agent(agent_id)
                continue
            path = find_jsonl(agent_id)
            if path is None:
                # Startup timeout: no JSONL after SPAWN_JSONL_TIMEOUT → agent failed to start
                spawn_time = agent_spawn_times.get(agent_id, 0)
                if spawn_time and (time.monotonic() - spawn_time) > SPAWN_JSONL_TIMEOUT:
                    logger.error("reaper: agent %s produced no JSONL after %ds, cleaning up", agent_id[:8], SPAWN_JSONL_TIMEOUT)
                    cleanup_agent(agent_id)
                else:
                    logger.debug("reaper: agent %s has no JSONL yet, skipping", agent_id[:8])
                continue
            last_entry = get_last_relevant_entry(path)
            if check_idle_done(agent_id, last_entry):
                logger.info("reaper: cleaning up agent %s", agent_id[:8])
                cleanup_agent(agent_id)


@router.get("/{agent_id}")
async def stream_agent(agent_id: str):
    # Wait up to 30s for the JSONL file to appear (agent startup takes time)
    path = find_jsonl(agent_id)
    if path is None:
        for _ in range(60):
            await asyncio.sleep(0.5)
            path = find_jsonl(agent_id)
            if path is not None:
                break
        if path is None:
            raise HTTPException(404, f"agent {agent_id} not found")

    async def stream():
        last_entry = {}
        idle_ticks = 0
        ping_interval = 15  # send keepalive every 15 idle ticks (30s)
        with open(path, "r") as f:
            while True:
                # Liveness & heartbeat checks
                if not _is_agent_process_alive(agent_id):
                    yield json.dumps({"type": "error", "error": "process_dead", "message": "Agent process is no longer running"}) + "\n"
                    cleanup_agent(agent_id)
                    break
                if _is_agent_heartbeat_stale(agent_id):
                    yield json.dumps({"type": "error", "error": "heartbeat_stale", "message": f"Agent heartbeat stale (>{HEARTBEAT_STALE_TIMEOUT}s)"}) + "\n"
                    cleanup_agent(agent_id)
                    break
                line = f.readline()
                if line:
                    idle_ticks = 0
                    yield line
                    try:
                        entry = json.loads(line)
                        prohibited = _check_prohibited_access(entry)
                        if prohibited:
                            logger.error("PROHIBITED_ACCESS agent=%s %s — killing agent", agent_id[:8], prohibited)
                            yield json.dumps({"type": "error", "error": "prohibited_access", "message": "Agent terminated due to policy violation"}) + "\n"
                            cleanup_agent(agent_id)
                            return
                        home_access = _check_home_access(entry)
                        if home_access:
                            logger.warning("HOME_ACCESS_ALERT agent=%s %s", agent_id[:8], home_access)
                            yield json.dumps({"type": "warning", "warning": "home_access", "message": f"Agent accessed sensitive path: {home_access}", "agent_id": agent_id}) + "\n"
                        if entry.get("type") in ("assistant", "result") and entry.get("timestamp"):
                            last_entry = entry
                    except (json.JSONDecodeError, AttributeError):
                        pass
                    if check_idle_done(agent_id, last_entry):
                        yield json.dumps({"type": "done"}) + "\n"
                        cleanup_agent(agent_id)
                        break
                else:
                    if check_idle_done(agent_id, last_entry):
                        yield json.dumps({"type": "done"}) + "\n"
                        cleanup_agent(agent_id)
                        break
                    idle_ticks += 1
                    if idle_ticks % ping_interval == 0:
                        if _is_agent_process_alive(agent_id):
                            yield json.dumps({"type": "heartbeat"}) + "\n"
                        else:
                            yield json.dumps({"type": "ping"}) + "\n"
                    await asyncio.sleep(2)
                    if not os.path.exists(path):
                        break

    return StreamingResponse(stream(), media_type="application/x-ndjson")


from fastapi import FastAPI

app = FastAPI(title="Codex Agent Spawner")
app.include_router(router)


@app.on_event("startup")
async def startup():
    asyncio.create_task(spawn_queue_worker())
    asyncio.create_task(reaper_loop())