"""Tests for presence_reader.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest

from agent_power.presence_reader import (
    STALE_AFTER_SECONDS,
    get_presence_dir,
    parse_presence_record,
    read_presence,
    working_agents,
)


FIXED_NOW = datetime(2026, 11, 17, 18, 0, 0, tzinfo=timezone.utc)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_valid_working_agent(tmp_path: Path) -> None:
    updated = (FIXED_NOW - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
    _write_json(
        tmp_path / "presence" / "claude.json",
        {
            "agent": "claude",
            "status": "working",
            "task": "compare databases",
            "pid": 12345,
            "updated_at": updated,
        },
    )

    entries = read_presence(tmp_path, now=FIXED_NOW)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.agent == "claude"
    assert entry.status == "working"
    assert entry.raw_status == "working"
    assert entry.task == "compare databases"
    assert entry.pid == 12345
    assert entry.is_stale is False
    assert entry.is_working is True
    assert entry.age_seconds == pytest.approx(30.0)


def test_staleness_boundary_120_seconds() -> None:
    # Exactly 120 seconds old is NOT older than 120 seconds -> still working
    at_boundary = parse_presence_record(
        {
            "agent": "codex",
            "status": "working",
            "task": "running suite",
            "pid": 222,
            "updated_at": (FIXED_NOW - timedelta(seconds=STALE_AFTER_SECONDS)).isoformat(),
        },
        now=FIXED_NOW,
    )
    assert at_boundary is not None
    assert at_boundary.status == "working"
    assert at_boundary.is_stale is False
    assert at_boundary.is_working is True

    # 120.1 seconds old IS older than 120 seconds -> stale -> counts as idle
    just_over = parse_presence_record(
        {
            "agent": "codex",
            "status": "working",
            "task": "running suite",
            "pid": 222,
            "updated_at": (FIXED_NOW - timedelta(seconds=120.1)).isoformat(),
        },
        now=FIXED_NOW,
    )
    assert just_over is not None
    assert just_over.status == "idle"
    assert just_over.raw_status == "working"
    assert just_over.is_stale is True
    assert just_over.is_working is False


def test_timezone_offsets_and_naive_timestamps() -> None:
    # -05:00 offset corresponding to FIXED_NOW - 45s (18:00 UTC is 13:00 -05:00)
    est_tz = timezone(timedelta(hours=-5))
    updated_est = (FIXED_NOW.astimezone(est_tz) - timedelta(seconds=45)).isoformat()
    rec_offset = parse_presence_record(
        {
            "agent": "gemini",
            "status": "working",
            "task": "refactoring policy",
            "pid": 999,
            "updated_at": updated_est,
        },
        now=FIXED_NOW,
    )
    assert rec_offset is not None
    assert rec_offset.is_working is True
    assert rec_offset.age_seconds == pytest.approx(45.0)
    assert rec_offset.updated_at.tzinfo == timezone.utc

    # Naive ISO string assumed UTC
    naive_str = (FIXED_NOW - timedelta(seconds=10)).replace(tzinfo=None).isoformat()
    rec_naive = parse_presence_record(
        {
            "agent": "gemini",
            "status": "working",
            "task": "naive timestamp",
            "pid": 999,
            "updated_at": naive_str,
        },
        now=FIXED_NOW,
    )
    assert rec_naive is not None
    assert rec_naive.is_working is True
    assert rec_naive.age_seconds == pytest.approx(10.0)


@pytest.mark.parametrize(
    "bad_payload",
    [
        None,
        [],
        "not a dict",
        {},
        {"agent": "", "status": "working", "task": "x", "pid": 1, "updated_at": "2026-11-17T18:00:00Z"},
        {"agent": "claude", "status": "busy", "task": "x", "pid": 1, "updated_at": "2026-11-17T18:00:00Z"},
        {"agent": "claude", "status": "working", "task": 123, "pid": 1, "updated_at": "2026-11-17T18:00:00Z"},
        {"agent": "claude", "status": "working", "task": "x", "pid": True, "updated_at": "2026-11-17T18:00:00Z"},
        {"agent": "claude", "status": "working", "task": "x", "pid": 0, "updated_at": "2026-11-17T18:00:00Z"},
        {"agent": "claude", "status": "working", "task": "x", "pid": -5, "updated_at": "2026-11-17T18:00:00Z"},
        {"agent": "claude", "status": "working", "task": "x", "pid": 10, "updated_at": "not-a-date"},
        {"agent": "claude", "status": "working", "task": "x", "pid": 10, "updated_at": ""},
    ],
)
def test_malformed_records_return_none(bad_payload: object) -> None:
    assert parse_presence_record(bad_payload, now=FIXED_NOW) is None


def test_read_presence_ignores_corrupt_and_non_files(tmp_path: Path) -> None:
    presence_dir = tmp_path / "presence"
    presence_dir.mkdir(parents=True)

    # Corrupt JSON
    (presence_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
    # Invalid binary bytes
    (presence_dir / "binary.json").write_bytes(b"\xff\xfe\x00\x00")
    # Directory ending in .json
    (presence_dir / "subdir.json").mkdir()
    # Valid idle agent
    _write_json(
        presence_dir / "idle_agent.json",
        {
            "agent": "aider",
            "status": "idle",
            "task": "done",
            "pid": 400,
            "updated_at": (FIXED_NOW - timedelta(minutes=10)).isoformat(),
            "prompt": "MUST NEVER BE READ OR EXPOSED",
        },
    )
    # Valid working agent
    _write_json(
        presence_dir / "active_agent.json",
        {
            "agent": "claude",
            "status": "working",
            "task": "writing unit tests",
            "pid": 401,
            "updated_at": (FIXED_NOW - timedelta(seconds=15)).isoformat(),
        },
    )

    entries = read_presence(tmp_path, now=FIXED_NOW)
    assert len(entries) == 2
    active = working_agents(entries)
    assert len(active) == 1
    assert active[0].agent == "claude"
    assert not hasattr(entries[0], "prompt")


def test_env_override_agent_room_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_ROOM_HOME", str(tmp_path))
    assert get_presence_dir() == tmp_path / "presence"
    assert read_presence(now=FIXED_NOW) == []
