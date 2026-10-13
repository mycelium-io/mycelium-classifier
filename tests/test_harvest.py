"""Tests for the harvest, on made-up rooms only. No room data belongs in this repository.

The ``mycelium`` CLI is replaced by a fake that writes export folders the way
``mycelium room export`` does, so these run without a hub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mycelium_classifier import harvest

ROOM = "demo"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _line(
    n: int,
    sender: str,
    text: str,
    *,
    kind: str = "agent",
    type_: str = "message",
    thread: str | None = None,
    minutes: int | None = None,
    mentions: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """One line as ``room export`` writes it."""
    return {
        "id": f"m{n}",
        "at": (T0 + timedelta(minutes=n if minutes is None else minutes)).isoformat(),
        "sender": sender,
        "sender_kind": kind,
        "type": type_,
        "notice": None,
        "channel": "thread" if thread else "live",
        "thread": thread,
        "task": "work/fix-login" if thread else None,
        "refers_to_thread": None,
        "refers_to_task": None,
        "parents": [],
        "mentions": mentions or [],
        "text": text,
        "data": {},
    }


class FakeCli:
    """Stands in for ``mycelium``: ``room ls`` and ``room export`` into a folder."""

    def __init__(self, lines: list[dict[str, Any]], board: list[dict[str, Any]] | None = None):
        self.lines = lines
        self.board = board or [{"key": "work/fix-login", "title": "Fix login", "thread": "t1"}]
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, settings: harvest.Settings, *args: str) -> Any:
        del settings
        self.calls.append(args)
        if args[:2] == ("room", "ls"):
            return [{"name": ROOM}]
        out = Path(args[args.index("--out") + 1])
        redacted = "--redact" in args
        out.mkdir(parents=True, exist_ok=True)
        cursor = out / "cursor.json"
        written = json.loads(cursor.read_text())["total"] if cursor.exists() else 0
        new = self.lines[written:]
        with (out / "messages.jsonl").open("a", encoding="utf-8") as fh:
            for line in new:
                if redacted:
                    line = {**line, "text": harvest.redact_text(line["text"])}
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        (out / "board.json").write_text(json.dumps(self.board), encoding="utf-8")
        cursor.write_text(json.dumps({"total": len(self.lines)}), encoding="utf-8")
        return {
            "room": ROOM,
            "new": len(new),
            "total": len(self.lines),
            "board_rows": len(self.board),
            "from_start": written == 0,
        }


def _settings(tmp_path: Path) -> harvest.Settings:
    return harvest.Settings(data=tmp_path / "data", hub=None, repo="o/r")


def test_an_export_line_gains_its_room_and_whether_to_label_it() -> None:
    agent = harvest.with_room(ROOM, _line(0, "builder", "done"))
    person = harvest.with_room(ROOM, _line(1, "ada", "thanks", kind="person"))
    notice = harvest.with_room(ROOM, _line(2, "system", "[filed] x", kind="system", type_="notice"))

    assert (agent["room"], agent["labelable"]) == (ROOM, True)
    assert person["labelable"] is False
    assert notice["labelable"] is False


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
    lines = [
        _line(
            0,
            "ada",
            "@builder can you look? @checker too",
            kind="person",
            minutes=0,
            mentions=[
                {"handle": "builder", "mode": "plain"},
                {"handle": "checker", "mode": "plain"},
            ],
        ),
        _line(1, "builder", "Done, and it's approved", minutes=5),
        _line(2, "checker", "I decided to keep the old flag", minutes=60),
        _line(3, "builder", "nothing to see", minutes=70),
    ]
    counts = harvest.candidates([harvest.with_room(ROOM, x) for x in lines])

    assert counts["C1_approved_or_ready_to_merge"] == 1
    assert counts["C2_done_fixed_green"] == 1
    assert counts["C3_decided_phrasing"] == 1
    # builder answered within 30 minutes; checker answered after an hour.
    assert (counts["C4_agent_mentions"], counts["C4_unanswered_30m"]) == (2, 1)


def test_a_run_exports_twice_and_writes_raw_redacted_stats_and_a_tally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    cli = FakeCli(
        [_line(0, "ada", "@builder hi a@b.co", kind="person"), _line(1, "builder", "done")]
    )
    monkeypatch.setattr(harvest, "mycelium", cli)
    monkeypatch.setattr(harvest, "prs", lambda _s: [])

    first = harvest.run(settings)
    cli.lines.append(_line(2, "builder", "fixed it", thread="t1"))
    second = harvest.run(settings)

    exports = [c for c in cli.calls if c[:2] == ("room", "export")]
    assert sum("--redact" in c for c in exports) == len(exports) // 2
    data = settings.data
    raw = harvest.read_jsonl(data / "raw" / f"{ROOM}.jsonl")
    redacted = harvest.read_jsonl(data / "redacted" / f"{ROOM}.jsonl")
    assert [r["id"] for r in raw] == ["m0", "m1", "m2"]
    assert [r["labelable"] for r in raw] == [False, True, True]
    assert "a@b.co" in raw[0]["text"]
    assert "<EMAIL>" in redacted[0]["text"]
    assert (data / "raw" / f"board-{ROOM}.json").exists()
    assert (first["new_records"], second["new_records"]) == (2, 1)
    tally = harvest.read_jsonl(data / "tally.jsonl")
    assert [t["new_agent_messages"] for t in tally] == [1, 1]
    assert tally[-1]["candidates"]["C2_done_fixed_green"] == 2


def test_a_room_that_fails_to_export_doesnt_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    cli = FakeCli([_line(0, "builder", "done")])

    def two_rooms(settings: harvest.Settings, *args: str) -> Any:
        if args[:2] == ("room", "ls"):
            return [{"name": "gone"}, {"name": ROOM}]
        if args[2] == "gone":
            raise RuntimeError("mycelium room export gone failed: Room not found")
        return cli(settings, *args)

    monkeypatch.setattr(harvest, "mycelium", two_rooms)
    monkeypatch.setattr(harvest, "prs", lambda _s: [])

    summary = harvest.run(settings)

    assert list(summary["failed"]) == ["gone"]
    assert [s["room"] for s in summary["rooms"]] == [ROOM]
    assert (settings.data / "raw" / f"{ROOM}.jsonl").exists()


def test_files_are_utf8_whatever_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        harvest, "mycelium", FakeCli([_line(0, "builder", "ship it 🚀, Zoë, 東京")])
    )
    monkeypatch.setattr(harvest, "prs", lambda _s: [])

    harvest.run(settings)

    raw = (settings.data / "raw" / f"{ROOM}.jsonl").read_bytes()
    assert "ship it 🚀, Zoë, 東京".encode() in raw


def test_settings_default_to_the_data_folder_outside_the_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MYCELIUM_CLASSIFIER_DATA", raising=False)
    settings = harvest.settings_from([])
    assert (settings.data, settings.hub) == (Path.home() / "Documents/mycelium-classifier", None)

    settings = harvest.settings_from(["--data", "/tmp/elsewhere", "--hub", "http://h:1/"])  # noqa: S108
    assert (settings.data, settings.hub) == (Path("/tmp/elsewhere"), "http://h:1")  # noqa: S108
