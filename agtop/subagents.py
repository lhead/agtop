"""Sub-agent scanner: parse subagents for a Claude Code session."""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .parser import (
    CLAUDE_DIR,
    WORKING_THRESHOLD,
    ACTIVE_THRESHOLD,
    _read_head_tail,
    _parse_claude_lines,
    _tool_summary,
    _parse_ts,
)

# Icon per agent type
_TYPE_ICONS: dict[str, str] = {
    "general-purpose": "◆",
    "Explore":         "🔍",
    "Plan":            "📋",
    "claude-code-guide": "📖",
    "statusline-setup": "⚙",
}
_DEFAULT_ICON = "◆"


@dataclass
class SubAgent:
    agent_id: str
    path: Path
    agent_type: str          # from meta.json agentType
    icon: str                # derived from agent_type
    prompt: str              # first user message (short summary)
    tool_summary: str        # last tool call
    status: str              # working | active | done
    start_ts: float          # file birthtime (approx spawn time)
    end_ts: Optional[float]  # file mtime when done, else None
    mtime: float             # last modification time
    spawn_index: int         # which Agent tool_use batch spawned this (0-based)
    parallel_index: int      # position within the batch (0-based)


@dataclass
class SpawnBatch:
    """A group of sub-agents spawned in the same assistant message."""
    index: int
    agents: list[SubAgent] = field(default_factory=list)


def _read_meta(meta_path: Path) -> dict:
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _agent_type_from_meta(meta_path: Path, agent_id: str) -> str:
    """Read agentType from the .meta.json file."""
    meta = _read_meta(meta_path)
    return meta.get("agentType", "general-purpose")


def _parse_agent_prompt(path: Path) -> str:
    """Extract the first user prompt sent to this sub-agent."""
    try:
        head_lines, _ = _read_head_tail(path, head_n=20, tail_bytes=0)
        for raw in head_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message", {})
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                text = " ".join(parts).strip()
            else:
                text = ""
            # Strip leading metadata lines like "Thoroughness: medium\n"
            text = re.sub(r'^(Thoroughness:\s*\S+|Type:\s*\S+)\s*\n', '', text, flags=re.IGNORECASE)
            return text.replace("\n", " ").strip()
    except Exception:
        pass
    return ""


def _parse_agent_last_tool(path: Path) -> str:
    """Extract the last tool call summary from a sub-agent JSONL."""
    try:
        _, tail_lines = _read_head_tail(path, head_n=0, tail_bytes=32768)
        last_summary = ""
        for raw in tail_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message", {})
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    s = _tool_summary(name, inp)
                    if s:
                        last_summary = s
        return last_summary
    except Exception:
        return ""


def _compute_agent_status(mtime: float, birthtime: float, now: float) -> str:
    age = now - mtime
    if age <= WORKING_THRESHOLD:
        return "working"
    if age <= ACTIVE_THRESHOLD:
        return "active"
    return "done"


def _parse_spawn_order(session_path: Path) -> dict[str, tuple[int, int]]:
    """
    Parse the main session JSONL to find the spawn order of sub-agents.

    Returns dict: agent_id → (spawn_index, parallel_index)

    Agents spawned in the same assistant message get the same spawn_index,
    and sequential parallel_index values.
    """
    order: dict[str, tuple[int, int]] = {}
    spawn_index = 0
    try:
        head_lines, tail_lines = _read_head_tail(session_path)
        for raw in head_lines + tail_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message", {})
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            # Collect all Agent tool_use calls in this message
            batch: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use" or block.get("name") != "Agent":
                    continue
                inp = block.get("input", {})
                desc = inp.get("description", "")
                # We match by description against subagent prompts later,
                # but agent_id isn't in the main session — use tool_use id as proxy
                tool_id = block.get("id", "")
                if tool_id:
                    batch.append(tool_id)
            if batch:
                # Store spawn_index; parallel_index = position in batch
                for parallel_idx, tool_id in enumerate(batch):
                    order[tool_id] = (spawn_index, parallel_idx)
                spawn_index += 1
    except Exception:
        pass
    return order


def scan_subagents(session_id: str, session_path: Path) -> list[SpawnBatch]:
    """
    Scan all sub-agents for a session and return them grouped as SpawnBatches.

    Falls back to mtime-based ordering if spawn order can't be determined.
    """
    subagents_dir = session_path.parent / session_id / "subagents"
    if not subagents_dir.exists():
        return []

    now = time.time()
    agents: list[SubAgent] = []

    for jsonl in sorted(subagents_dir.glob("*.jsonl")):
        if jsonl.name.endswith(".meta.json"):
            continue
        agent_id = jsonl.stem  # e.g. "agent-a24895d2ad7822ef0"

        try:
            st = jsonl.stat()
            mtime = st.st_mtime
            birthtime = getattr(st, "st_birthtime", mtime)
        except OSError:
            continue

        meta_path = jsonl.with_suffix(".meta.json")
        # Drop "agent-" prefix for meta lookup
        raw_id = agent_id.removeprefix("agent-")
        if not meta_path.exists():
            meta_path = subagents_dir / f"{agent_id}.meta.json"

        agent_type = _agent_type_from_meta(meta_path, raw_id)
        icon = _TYPE_ICONS.get(agent_type, _DEFAULT_ICON)

        prompt = _parse_agent_prompt(jsonl)
        tool_sum = _parse_agent_last_tool(jsonl)
        status = _compute_agent_status(mtime, birthtime, now)

        end_ts = mtime if status == "done" else None

        agents.append(SubAgent(
            agent_id=agent_id,
            path=jsonl,
            agent_type=agent_type,
            icon=icon,
            prompt=prompt,
            tool_summary=tool_sum,
            status=status,
            start_ts=birthtime,
            end_ts=end_ts,
            mtime=mtime,
            spawn_index=0,    # filled below
            parallel_index=0, # filled below
        ))

    if not agents:
        return []

    # Sort by birthtime to infer spawn order
    agents.sort(key=lambda a: a.start_ts)

    # Group into batches: agents born within 2 seconds of each other = same batch
    BATCH_GAP = 2.0
    batches: list[list[SubAgent]] = []
    current_batch: list[SubAgent] = [agents[0]]

    for agent in agents[1:]:
        if agent.start_ts - current_batch[-1].start_ts <= BATCH_GAP:
            current_batch.append(agent)
        else:
            batches.append(current_batch)
            current_batch = [agent]
    batches.append(current_batch)

    # Assign spawn_index and parallel_index
    result: list[SpawnBatch] = []
    for spawn_idx, batch_agents in enumerate(batches):
        for par_idx, agent in enumerate(batch_agents):
            agent.spawn_index = spawn_idx
            agent.parallel_index = par_idx
        result.append(SpawnBatch(index=spawn_idx, agents=batch_agents))

    return result
