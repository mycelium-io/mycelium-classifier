"""Collect a hub's room histories into a local dataset for labeling.

Reads every room on the hub: each room's transcript (``log/transcript.jsonl`` in
the hub's data folder, read-only), its board rows and its members (over the
hub's API), and the pull requests of the repository the rooms work on (``gh``,
read-only). Writes, into the data folder:

    raw/<room>.jsonl         one record per message, notices and pings included
    raw/board-<room>.json    every board row (work/, decisions/, status/, failed/)
    raw/prs.json             pull request state and reviews
    redacted/...             the same, with emails, tokens, links other than
                             GitHub's and home-folder names replaced
    stats.json               counts per room, and rule-only candidate counts
    tally.jsonl              one line per run, for how fast each class grows
    state.json               a cursor per room, so a run reads only what is new

It reads the hub's files, so it runs on the hub's machine. Standard library only.

    python3 -m mycelium_classifier.harvest [--data DIR] [--hub URL] [--hub-data DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

BOARD_PREFIXES = ("work/", "decisions/", "status/", "failed/")
PROSE_TYPES = frozenset({"message", "reply"})
SYSTEM_TYPES = ("notice", "ping", "extraction")
C4_WINDOW = timedelta(minutes=30)
MEMORY_PAGE = 500

MENTION = re.compile(r"(?<![\w`/.])@([!~]?)([A-Za-z0-9][A-Za-z0-9_-]*)")
MENTION_MODES = {"!": "urgent", "~": "silent"}
ROLE_KINDS = {"human": "person", "agent": "agent", "coordinator": "engine"}


@dataclass
class Settings:
    data: Path
    hub: str
    hub_data: Path
    repo: str


def parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def short_id(urn: str | None) -> str | None:
    """``urn:ioc:mycelium:episode:<room>:<id>`` → ``<id>``."""
    return urn.rsplit(":", 1)[-1] if urn else None


# ── Reading the hub ─────────────────────────────────────────────────────────


def api(settings: Settings, path: str, **params: Any) -> Any:
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    with urllib.request.urlopen(f"{settings.hub}/api{path}{query}", timeout=30) as resp:  # noqa: S310
        return json.load(resp)


def roster(settings: Settings, room: str) -> dict[str, str]:
    """Handle → ``person`` or ``agent``. Engines aren't listed; their role places them."""
    kinds: dict[str, str] = {"system": "system"}
    for agent in api(settings, f"/rooms/{room}/agents"):
        if agent.get("handle"):
            kinds[agent["handle"]] = "agent"
    users = api(settings, "/users")
    for user in users.get("users", []) if isinstance(users, dict) else users:
        if user.get("handle"):
            kinds[user["handle"]] = "person"
    return kinds


def board(settings: Settings, room: str) -> list[dict[str, Any]]:
    rows = []
    for prefix in BOARD_PREFIXES:
        offset = 0
        while True:
            page = api(
                settings, f"/rooms/{room}/memory", prefix=prefix, limit=MEMORY_PAGE, offset=offset
            )
            for mem in page:
                text = mem.get("content_text") or ""
                rows.append(
                    {
                        "key": mem["key"],
                        "title": text.split("\n", 1)[0],
                        "body": text,
                        "thread": short_id(mem.get("episode")),
                        "meta": mem.get("meta") or {},
                        "created_by": mem.get("created_by"),
                        "updated_by": mem.get("updated_by"),
                        "version": mem.get("version"),
                        "created_at": mem.get("created_at"),
                        "updated_at": mem.get("updated_at"),
                    }
                )
            if len(page) < MEMORY_PAGE:
                break
            offset += MEMORY_PAGE
    return rows


def read_transcript(
    settings: Settings, room: str, cursor: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Records after the cursor, the new cursor, and whether the room was read from the start.

    The transcript is append-only, so the cursor is a line count plus the id on
    that line. If the file got shorter or that line no longer holds that id, the
    room is read again from the start.
    """
    path = settings.hub_data / "rooms" / room / "log" / "transcript.jsonl"
    if not path.exists():
        had_lines = cursor is not None and cursor.get("lines", 0) > 0
        return [], {"lines": 0, "last_id": None}, cursor is None or had_lines
    with path.open(encoding="utf-8") as fh:
        lines = [line for line in fh if line.strip()]
    start = 0
    if (
        cursor
        and 0 < cursor["lines"] <= len(lines)
        and json.loads(lines[cursor["lines"] - 1]).get("message_id") == cursor["last_id"]
    ):
        start = cursor["lines"]
    records = [json.loads(line) for line in lines[start:]]
    last_id = json.loads(lines[-1]).get("message_id") if lines else None
    return records, {"lines": len(lines), "last_id": last_id}, start == 0


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


# ── One transcript record → one line ────────────────────────────────────────


def notice_text(data: dict[str, Any]) -> str:
    sub = data.get("subkind")
    title = data.get("title") or data.get("key") or ""
    if sub == "upstream":
        head = f"[upstream] {data.get('label') or ''} {data.get('change') or ''}".rstrip()
        return f"{head}: {title} {data.get('url') or ''}".strip()
    by = f" by @{data['by']}" if data.get("by") else ""
    for_ = f" for @{data['for']}" if data.get("for") else ""
    return f"[{sub}] {title}{by}{for_}"


def mentions(text: str) -> list[dict[str, str]]:
    """Every ``@handle``, with how it was said: plain, silent ``@~`` or urgent ``@!``."""
    return [
        {"handle": handle, "mode": MENTION_MODES.get(mode, "plain")}
        for mode, handle in MENTION.findall(text)
    ]


def normalize(
    room: str, rec: dict[str, Any], kinds: dict[str, str], task_of: dict[str, str]
) -> dict[str, Any]:
    content = rec.get("content") or {}
    l9 = content.get("l9") or {}
    header = l9.get("header") or {}
    message = header.get("message") or {}
    payload = l9.get("payload") or {}
    data = dict(payload.get("data") or {})
    ptype = payload.get("type") or rec.get("kind")
    sender = rec.get("sender") or ""
    actors = (header.get("participants") or {}).get("actors") or []
    role = next((a.get("role") for a in actors if a.get("id") == sender), None)
    kind = kinds.get(sender) or ROLE_KINDS.get(role or "", "unknown")

    refers_to = None
    if ptype == "notice":
        text = notice_text(data)
        refers_to = short_id(data.get("episode"))
    elif ptype == "ping":
        text = f"[ping] @{data.get('sender')} wrote in {short_id(data.get('episode'))}"
        refers_to = short_id(data.get("episode"))
    else:
        text = content.get("content") or ""
        if ptype == "extraction":
            data.pop("content", None)

    thread = short_id(message.get("episode"))
    threaded = thread not in (None, "live", "knowledge")
    return {
        "room": room,
        "id": rec.get("message_id"),
        "at": rec.get("recorded_at"),
        "sender": sender,
        "sender_kind": kind,
        "l9_role": role,
        "type": ptype,
        "notice": data.get("subkind") if ptype == "notice" else None,
        "channel": "thread" if threaded else (thread or "live"),
        "thread": thread if threaded else None,
        "task": task_of.get(thread) if threaded and thread else None,
        "refers_to_thread": refers_to,
        "refers_to_task": task_of.get(refers_to) if refers_to else None,
        "parents": message.get("parents") or [],
        "mentions": mentions(text) if ptype in PROSE_TYPES else [],
        "text": text,
        "data": data,
        "labelable": kind == "agent" and ptype in PROSE_TYPES,
    }


# ── Redaction ───────────────────────────────────────────────────────────────

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


def stats(room: str, records: list[dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"room": room, "records": 0, "board_rows": len(rows)}
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
        "board_rows": len(rows),
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


def harvest_room(
    settings: Settings, room: str, cursor: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append what's new in one room; its stats and its new cursor."""
    out = settings.data
    kinds = roster(settings, room)
    rows = board(settings, room)
    task_of = {row["thread"]: row["key"] for row in rows if row["thread"]}
    fresh, new_cursor, from_start = read_transcript(settings, room, cursor)
    new = sorted((normalize(room, rec, kinds, task_of) for rec in fresh), key=lambda r: r["at"])
    write_jsonl(out / "raw" / f"{room}.jsonl", new, append=not from_start)
    write_jsonl(out / "redacted" / f"{room}.jsonl", redact(new), append=not from_start)
    write_json(out / "raw" / f"board-{room}.json", rows)
    write_json(out / "redacted" / f"board-{room}.json", redact(rows))
    room_stats = stats(room, read_jsonl(out / "raw" / f"{room}.jsonl"), rows)
    room_stats["new_records"] = len(new)
    room_stats["new_agent_messages"] = sum(r["labelable"] for r in new)
    print(f"{room}: +{len(new)} ({'from the start' if from_start else 'appended'})")
    return room_stats, new_cursor


def run(settings: Settings) -> dict[str, Any]:
    out = settings.data
    state_path = out / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    cursors = state.setdefault("cursors", {})
    all_stats = []
    for room in [r["name"] for r in api(settings, "/rooms")]:
        room_stats, cursors[room] = harvest_room(settings, room, cursors.get(room))
        all_stats.append(room_stats)
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
    state["last_run"] = now
    write_json(state_path, state)
    return summary


def settings_from(argv: list[str] | None = None) -> Settings:
    env = os.environ
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--data",
        default=env.get("MYCELIUM_CLASSIFIER_DATA", Path.home() / "Documents/mycelium-classifier"),
        help="Where the dataset lives, outside any repository (MYCELIUM_CLASSIFIER_DATA)",
    )
    parser.add_argument(
        "--hub",
        default=env.get("MYCELIUM_API_URL", "http://127.0.0.1:8000"),
        help="The hub's address (MYCELIUM_API_URL)",
    )
    parser.add_argument(
        "--hub-data",
        default=env.get("MYCELIUM_DATA_DIR", Path.home() / ".mycelium"),
        help="The hub's data folder, where each room's transcript is (MYCELIUM_DATA_DIR)",
    )
    parser.add_argument(
        "--repo",
        default="mycelium-io/mycelium",
        help="The GitHub repository whose pull requests the rooms talk about",
    )
    args = parser.parse_args(argv)
    return Settings(
        data=Path(args.data).expanduser(),
        hub=str(args.hub).rstrip("/"),
        hub_data=Path(args.hub_data).expanduser(),
        repo=args.repo,
    )


def main(argv: list[str] | None = None) -> None:
    run(settings_from(argv))


if __name__ == "__main__":
    main()
