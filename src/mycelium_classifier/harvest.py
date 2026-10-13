"""Collect a hub's room histories into a local dataset for labeling.

For every room on the hub, runs ``mycelium room export`` twice: as written, and
with ``--redact``. Each export folder keeps its own cursor, so a run fetches only
what is new. Also reads the pull requests of the repository the rooms work on
(``gh``, read-only). Writes, into the data folder:

    exports/<room>/          the export as written (messages.jsonl, board.json, cursor.json)
    exports-redacted/<room>/ the same with emails, tokens, links other than GitHub's
                             and home-folder names replaced
    raw/<room>.jsonl         the export's messages, each with its room and whether
                             it is an agent's message to label (``labelable``)
    raw/board-<room>.json    the room's board rows
    raw/prs.json             pull request state and reviews
    redacted/...             the same, from the redacted export
    stats.json               counts per room, and rule-only candidate counts
    tally.jsonl              one line per run, for how fast each class grows

It reads the hub over HTTP through the ``mycelium`` CLI, so it runs on any
machine that can reach the hub. Standard library only, plus the CLI (3.0.43 or newer).

    python3 -m mycelium_classifier.harvest [--data DIR] [--hub URL] [--repo OWNER/NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROSE_TYPES = frozenset({"message", "reply"})
SYSTEM_TYPES = ("notice", "ping", "extraction")
C4_WINDOW = timedelta(minutes=30)


@dataclass
class Settings:
    data: Path
    hub: str | None
    repo: str


def parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# ── The CLI ─────────────────────────────────────────────────────────────────


def mycelium(settings: Settings, *args: str) -> Any:
    """Run a ``mycelium`` command with ``--json`` and return what it printed."""
    env = dict(os.environ)
    if settings.hub:
        env["MYCELIUM_API_URL"] = settings.hub
    out = subprocess.run(  # noqa: S603
        ["mycelium", *args, "--json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if out.returncode != 0:
        msg = f"mycelium {' '.join(args)} failed: {out.stderr.strip() or out.stdout.strip()}"
        raise RuntimeError(msg)
    return json.loads(out.stdout)


def rooms(settings: Settings) -> list[str]:
    return [r["name"] for r in mycelium(settings, "room", "ls")]


def export(settings: Settings, room: str, out: Path, *, redact: bool) -> dict[str, Any]:
    args = ["room", "export", room, "--out", str(out)]
    if redact:
        args.append("--redact")
    return mycelium(settings, *args)


def prs(settings: Settings) -> list[dict[str, Any]]:
    fields = (
        "number,title,state,createdAt,mergedAt,closedAt,reviewDecision,reviews,author,url,"
        "headRefName"
    )
    command = ["gh", "pr", "list", "-R", settings.repo, "--state", "all", "--limit", "1000"]
    out = subprocess.run(  # noqa: S603
        [*command, "--json", fields],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        print(f"  gh: {out.stderr.strip()}", file=sys.stderr)
        return []
    return json.loads(out.stdout)


# ── From an export to the dataset ───────────────────────────────────────────


def with_room(room: str, record: dict[str, Any]) -> dict[str, Any]:
    """An export line, with its room and whether it is an agent's message to label."""
    return {
        "room": room,
        **record,
        "labelable": record.get("sender_kind") == "agent" and record.get("type") in PROSE_TYPES,
    }


def derive(room: str, export_dir: Path, into: Path) -> list[dict[str, Any]]:
    """Write ``into/<room>.jsonl`` and ``into/board-<room>.json`` from an export folder."""
    records = [with_room(room, r) for r in read_jsonl(export_dir / "messages.jsonl")]
    write_jsonl(into / f"{room}.jsonl", records)
    board = export_dir / "board.json"
    if board.exists():
        into.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(board, into / f"board-{room}.json")
    return records


# ── Redaction, for the PR list (exports redact themselves) ──────────────────

REDACTIONS = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<EMAIL>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"), "<TOKEN>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "<TOKEN>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<TOKEN>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<TOKEN>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<TOKEN>"),
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*)['\"]?[^\s'\"`,;]{8,}"
        ),
        r"\1<TOKEN>",
    ),
    (re.compile(r"/Users/[^/\s`'\"]+"), "/Users/<USER>"),
    (re.compile(r"/home/[^/\s`'\"]+"), "/home/<USER>"),
]
URL = re.compile(r"\bhttps?://[^\s)>\]`'\"]+")
KEPT_HOSTS = ("github.com", "githubusercontent.com", "localhost", "127.0.0.1")


def _url(match: re.Match[str]) -> str:
    host = urllib.parse.urlsplit(match.group(0)).hostname or ""
    if any(host == kept or host.endswith("." + kept) for kept in KEPT_HOSTS):
        return match.group(0)
    return "<URL>"


def redact_text(text: str) -> str:
    text = URL.sub(_url, text)
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


# ── Rule-only candidate counts (counts, not labels) ─────────────────────────

C1 = re.compile(r"(?i)\b(approved|ready to merge)\b")
C2 = re.compile(r"(?i)\b(done|fixed|green)\b")
C3 = re.compile(r"(?i)\b(I (?:decided|chose|went with|picked)|decided to|going with)\b")


def candidates(records: list[dict[str, Any]]) -> dict[str, int]:
    """Messages each rough rule would put in front of a labeler. Not labels."""
    prose = [r for r in records if r["type"] in PROSE_TYPES]
    agents = [r for r in prose if r["labelable"]]
    counts = {
        "C1_approved_or_ready_to_merge": sum(bool(C1.search(r["text"])) for r in agents),
        "C2_done_fixed_green": sum(bool(C2.search(r["text"])) for r in agents),
        "C3_decided_phrasing": sum(bool(C3.search(r["text"])) for r in agents),
    }
    spoke_at: dict[str, list[datetime]] = defaultdict(list)
    for r in prose:
        spoke_at[r["sender"]].append(parse_time(r["at"]))
    kind_of = {r["sender"]: r["sender_kind"] for r in records}
    asked = unanswered = 0
    for r in prose:
        at = parse_time(r["at"])
        for m in r["mentions"]:
            handle = m["handle"]
            if m["mode"] == "silent" or handle == r["sender"] or kind_of.get(handle) != "agent":
                continue
            asked += 1
            if not any(at < s <= at + C4_WINDOW for s in spoke_at.get(handle, [])):
                unanswered += 1
    counts["C4_agent_mentions"] = asked
    counts["C4_unanswered_30m"] = unanswered
    return counts


def stats(room: str, records: list[dict[str, Any]], board_rows: int) -> dict[str, Any]:
    if not records:
        return {"room": room, "records": 0, "board_rows": board_rows}
    types = Counter(r["type"] for r in records)
    agent_prose = [r for r in records if r["labelable"]]
    system_lines = sum(types[t] for t in SYSTEM_TYPES)
    return {
        "room": room,
        "records": len(records),
        "first": min(r["at"] for r in records),
        "last": max(r["at"] for r in records),
        "types": dict(types),
        "prose_by_sender_kind": dict(
            Counter(r["sender_kind"] for r in records if r["type"] in PROSE_TYPES)
        ),
        "agent_messages_labelable": len(agent_prose),
        "agent_messages_in_threads": sum(r["channel"] == "thread" for r in agent_prose),
        "agents": dict(Counter(r["sender"] for r in agent_prose)),
        "system_lines": system_lines,
        "system_share": round(system_lines / len(records), 3),
        "threads_with_prose": len(
            {r["thread"] for r in records if r["thread"] and r["type"] in PROSE_TYPES}
        ),
        "board_rows": board_rows,
        "candidates": candidates(records),
    }


# ── Files ───────────────────────────────────────────────────────────────────


def write_jsonl(path: Path, items: list[dict[str, Any]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


# ── A run ───────────────────────────────────────────────────────────────────


def harvest_room(settings: Settings, room: str) -> dict[str, Any]:
    """Bring one room's two exports up to date and derive its dataset files."""
    out = settings.data
    plain = export(settings, room, out / "exports" / room, redact=False)
    export(settings, room, out / "exports-redacted" / room, redact=True)
    records = derive(room, out / "exports" / room, out / "raw")
    derive(room, out / "exports-redacted" / room, out / "redacted")
    room_stats = stats(room, records, int(plain.get("board_rows", 0)))
    new = int(plain.get("new", 0))
    room_stats["new_records"] = new
    room_stats["new_agent_messages"] = sum(r["labelable"] for r in records[len(records) - new :])
    print(f"{room}: +{new} ({'from the start' if plain.get('from_start') else 'appended'})")
    return room_stats


def run(settings: Settings) -> dict[str, Any]:
    out = settings.data
    all_stats = [harvest_room(settings, room) for room in rooms(settings)]
    pulls = prs(settings)
    write_json(out / "raw" / "prs.json", pulls)
    write_json(out / "redacted" / "prs.json", redact(pulls))

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    summary = {
        "at": now,
        "new_records": sum(s.get("new_records", 0) for s in all_stats),
        "rooms": all_stats,
        "prs": len(pulls),
    }
    write_json(out / "stats.json", summary)
    tally: Counter[str] = Counter()
    for s in all_stats:
        tally.update(s.get("candidates", {}))
    write_jsonl(
        out / "tally.jsonl",
        [
            {
                "at": now,
                "agent_messages": sum(s.get("agent_messages_labelable", 0) for s in all_stats),
                "new_agent_messages": sum(s.get("new_agent_messages", 0) for s in all_stats),
                "candidates": dict(tally),
            }
        ],
        append=True,
    )
    return summary


def settings_from(argv: list[str] | None = None) -> Settings:
    env = os.environ
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument(
        "--data",
        default=env.get("MYCELIUM_CLASSIFIER_DATA", Path.home() / "Documents/mycelium-classifier"),
        help="Where the dataset lives, outside any repository (MYCELIUM_CLASSIFIER_DATA)",
    )
    parser.add_argument(
        "--hub",
        default=None,
        help="The hub's address, if not the one the mycelium CLI is set up for",
    )
    parser.add_argument(
        "--repo",
        default="mycelium-io/mycelium",
        help="The GitHub repository whose pull requests the rooms talk about",
    )
    args = parser.parse_args(argv)
    return Settings(
        data=Path(args.data).expanduser(),
        hub=str(args.hub).rstrip("/") if args.hub else None,
        repo=args.repo,
    )


def main(argv: list[str] | None = None) -> None:
    run(settings_from(argv))


if __name__ == "__main__":
    main()
