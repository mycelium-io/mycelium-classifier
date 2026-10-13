"""Tests for the harvest, on made-up rooms only. No room data belongs in this repository."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mycelium_classifier import harvest

ROOM = "demo"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _rec(
    n: int,
    sender: str,
    text: str | None = None,
    *,
    ptype: str = "message",
    episode: str = "live",
    role: str = "agent",
    data: dict[str, Any] | None = None,
    minutes: int | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "l9": {
            "header": {
                "message": {
                    "episode": f"urn:ioc:mycelium:episode:{ROOM}:{episode}",
                    "id": f"m{n}",
                    "parents": [],
                },
                "participants": {"actors": [{"id": sender, "role": role}]},
            },
            "payload": {"type": ptype, "data": data or {}},
        }
    }
    if text is not None:
        content["content"] = text
    return {
        "content": content,
        "kind": "exchange",
        "message_id": f"m{n}",
        "recorded_at": (T0 + timedelta(minutes=n if minutes is None else minutes)).isoformat(),
        "sender": sender,
        "subkind": None,
    }


KINDS = {"ada": "person", "builder": "agent", "checker": "agent", "system": "system"}
TASKS = {"t1": "work/fix-login"}


def test_a_record_says_who_where_and_what() -> None:
    ask = harvest.normalize(
        ROOM, _rec(0, "ada", "@builder go @~checker fyi @!builder now", role="human"), KINDS, TASKS
    )
    reply = harvest.normalize(ROOM, _rec(1, "builder", "on it", episode="t1"), KINDS, TASKS)
    notice = harvest.normalize(
        ROOM,
        _rec(
            2,
            "system",
            ptype="notice",
            role="coordinator",
            data={
                "subkind": "resolved",
                "title": "Fix login",
                "by": "builder",
                "episode": f"urn:ioc:mycelium:episode:{ROOM}:t1",
            },
        ),
        KINDS,
        TASKS,
    )
    engine = harvest.normalize(ROOM, _rec(3, "aligner", "round one", role="coordinator"), KINDS, {})

    assert (ask["sender_kind"], ask["channel"], ask["labelable"]) == ("person", "live", False)
    assert [m["mode"] for m in ask["mentions"]] == ["plain", "silent", "urgent"]
    assert (reply["thread"], reply["task"], reply["labelable"]) == ("t1", "work/fix-login", True)
    assert notice["text"] == "[resolved] Fix login by @builder"
    assert (notice["notice"], notice["refers_to_task"], notice["labelable"]) == (
        "resolved",
        "work/fix-login",
        False,
    )
    assert engine["sender_kind"] == "engine"


def test_redact_replaces_secrets_and_keeps_github() -> None:
    text = (
        "mail a@b.co, see https://example.com/x and https://github.com/o/r/pull/1, "
        "token ghp_abcdefghijklmnopqrstuvwxyz0123, password=hunter2hunter2, in /Users/someone/code"
    )
    assert harvest.redact_text(text) == (
        "mail <EMAIL>, see <URL> and https://github.com/o/r/pull/1, "
        "token <TOKEN>, password=<TOKEN>, in /Users/<USER>/code"
    )


def test_candidate_counts_follow_the_rules() -> None:
    records = [
        harvest.normalize(ROOM, r, KINDS, TASKS)
        for r in [
            _rec(0, "ada", "@builder can you look? @checker too", role="human", minutes=0),
            _rec(1, "builder", "Done, and it's approved", minutes=5),
            _rec(2, "checker", "I decided to keep the old flag", minutes=60),
            _rec(3, "builder", "nothing to see", minutes=70),
        ]
    ]
    counts = harvest.candidates(records)

    assert counts["C1_approved_or_ready_to_merge"] == 1
    assert counts["C2_done_fixed_green"] == 1
    assert counts["C3_decided_phrasing"] == 1
    # builder answered within 30 minutes; checker answered after an hour.
    assert (counts["C4_agent_mentions"], counts["C4_unanswered_30m"]) == (2, 1)


def _write_transcript(hub_data: Path, records: list[dict[str, Any]]) -> None:
    path = hub_data / "rooms" / ROOM / "log" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _settings(tmp_path: Path) -> harvest.Settings:
    return harvest.Settings(
        data=tmp_path / "data", hub="http://hub", hub_data=tmp_path / "hub", repo="o/r"
    )


def test_the_cursor_reads_only_what_is_new(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    records = [_rec(n, "builder", f"msg {n}") for n in range(3)]
    _write_transcript(settings.hub_data, records)

    fresh, cursor, from_start = harvest.read_transcript(settings, ROOM, None)
    assert (len(fresh), cursor["lines"], from_start) == (3, 3, True)

    _write_transcript(settings.hub_data, [*records, _rec(3, "builder", "msg 3")])
    fresh, cursor, from_start = harvest.read_transcript(settings, ROOM, cursor)
    assert ([r["message_id"] for r in fresh], from_start) == (["m3"], False)

    _write_transcript(settings.hub_data, [_rec(9, "builder", "rewritten")])
    fresh, _, from_start = harvest.read_transcript(settings, ROOM, cursor)
    assert ([r["message_id"] for r in fresh], from_start) == (["m9"], True)


def _fake_api(settings: harvest.Settings, path: str, **_params: Any) -> Any:
    del settings
    if path == "/rooms":
        return [{"name": ROOM}]
    if path.endswith("/agents"):
        return [{"handle": "builder"}, {"handle": "checker"}]
    if path == "/users":
        return {"users": [{"handle": "ada"}]}
    return []


def test_a_run_writes_raw_redacted_stats_and_a_tally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(harvest, "api", _fake_api)
    monkeypatch.setattr(harvest, "prs", lambda _s: [])
    _write_transcript(
        settings.hub_data,
        [_rec(0, "ada", "@builder hi a@b.co", role="human"), _rec(1, "builder", "done")],
    )

    first = harvest.run(settings)
    second = harvest.run(settings)

    data = settings.data
    raw = harvest.read_jsonl(data / "raw" / f"{ROOM}.jsonl")
    redacted = harvest.read_jsonl(data / "redacted" / f"{ROOM}.jsonl")
    assert [r["sender_kind"] for r in raw] == ["person", "agent"]
    assert "a@b.co" in raw[0]["text"]
    assert "<EMAIL>" in redacted[0]["text"]
    assert (first["new_records"], second["new_records"]) == (2, 0)
    tally = harvest.read_jsonl(data / "tally.jsonl")
    assert [t["agent_messages"] for t in tally] == [1, 1]
    assert tally[-1]["candidates"]["C2_done_fixed_green"] == 1


def test_settings_default_to_the_data_folder_outside_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MYCELIUM_CLASSIFIER_DATA", raising=False)
    settings = harvest.settings_from([])
    assert settings.data == Path.home() / "Documents/mycelium-classifier"

    settings = harvest.settings_from(["--data", "/tmp/elsewhere", "--hub", "http://h:1/"])  # noqa: S108
    assert (settings.data, settings.hub) == (Path("/tmp/elsewhere"), "http://h:1")  # noqa: S108
