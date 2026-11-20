"""Presence file reader for the multi-agent-memory-hub room contract.

Reads ~/.agent-room/presence/<agent>.json (or $AGENT_ROOM_HOME/presence/<agent>.json).
Only extracts the contract fields (agent, status, task, pid, updated_at).
Marks any "working" entry older than 120 seconds as stale ("idle").
Silently skips missing directories, unreadable files, and malformed records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Iterable

STALE_AFTER_SECONDS: float = 120.0
MAX_FUTURE_SKEW_SECONDS: float = 5.0
VALID_STATUSES: frozenset[str] = frozenset({"working", "idle"})
ALLOWED_TASK_MAX_CHARS: int = 280


@dataclass(frozen=True)
class AgentPresence:
    """Validated presence state for a single agent in the room."""

    agent: str
    status: str  # Effective status: "working" or "idle" (after staleness check)
    raw_status: str  # Original status recorded in the JSON file
    task: str
    pid: int
    updated_at: datetime
    age_seconds: float
    is_stale: bool

    @property
    def is_working(self) -> bool:
        """Return True if this agent is currently considered working."""
        return self.status == "working"


def get_presence_dir(room_home: Path | str | None = None) -> Path:
    """Resolve the presence directory from an explicit path or AGENT_ROOM_HOME."""
    if room_home is not None:
        base = Path(room_home).expanduser()
    else:
        env_home = os.environ.get("AGENT_ROOM_HOME", "").strip()
        base = Path(env_home).expanduser() if env_home else Path("~/.agent-room").expanduser()
    return base / "presence"


def _parse_iso_utc(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp string and normalize to UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_presence_record(
    raw_data: object,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = STALE_AFTER_SECONDS,
    max_future_seconds: float | None = None,
) -> AgentPresence | None:
    """Validate a decoded JSON object against the presence contract.

    Returns None if any required field is missing or malformed.
    Never inspects fields outside (agent, status, task, pid, updated_at).
    """
    if not isinstance(raw_data, dict):
        return None

    agent = raw_data.get("agent")
    raw_status = raw_data.get("status")
    task = raw_data.get("task")
    pid = raw_data.get("pid")
    updated_at_raw = raw_data.get("updated_at")

    if not isinstance(agent, str) or not agent.strip():
        return None
    if not isinstance(raw_status, str) or raw_status not in VALID_STATUSES:
        return None
    if not isinstance(task, str):
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None

    updated_at = _parse_iso_utc(updated_at_raw)
    if updated_at is None:
        return None

    ref_now = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    age_seconds = (ref_now - updated_at).total_seconds()
    is_future_stale = (
        max_future_seconds is not None and age_seconds < -max_future_seconds
    )
    is_stale = raw_status == "working" and (
        age_seconds > stale_after_seconds or is_future_stale
    )
    effective_status = "idle" if is_stale else raw_status

    clean_task = " ".join(task.strip().split())[:ALLOWED_TASK_MAX_CHARS]

    return AgentPresence(
        agent=agent.strip(),
        status=effective_status,
        raw_status=raw_status,
        task=clean_task,
        pid=pid,
        updated_at=updated_at,
        age_seconds=age_seconds,
        is_stale=is_stale,
    )


def read_presence(
    room_home: Path | str | None = None,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = STALE_AFTER_SECONDS,
    max_future_seconds: float | None = None,
) -> list[AgentPresence]:
    """Read and validate all agent presence files in the room directory."""
    presence_dir = get_presence_dir(room_home)
    if not presence_dir.exists() or not presence_dir.is_dir():
        return []

    ref_now = now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    results: list[AgentPresence] = []

    try:
        paths = sorted(presence_dir.glob("*.json"))
    except OSError:
        return []

    for path in paths:
        if not path.is_file():
            continue
        try:
            raw_text = path.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue

        record = parse_presence_record(
            raw_data,
            now=ref_now,
            stale_after_seconds=stale_after_seconds,
            max_future_seconds=max_future_seconds,
        )
        if record is not None:
            results.append(record)

    return results


def working_agents(entries: Iterable[AgentPresence]) -> list[AgentPresence]:
    """Filter presence entries down to agents currently considered working."""
    return [entry for entry in entries if entry.is_working]
