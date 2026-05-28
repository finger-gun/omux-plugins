#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import html
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


HOME = Path.home()
OMUX_DB = HOME / ".omux" / "agent-sessions.sqlite"
CODEX_DB = HOME / ".codex" / "state_5.sqlite"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
COPILOT_ROOT = HOME / ".copilot" / "session-state"
OMP_ROOT = HOME / ".omp" / "agent" / "sessions"


@dataclass(frozen=True)
class ScopedSession:
    agent: str
    session_id: str
    title: str
    cwd: str | None
    source_path: str | None
    updated_at_ms: int


@dataclass
class UsageRecord:
    agent: str
    session_id: str
    title: str
    cwd: str | None
    model: str
    total_tokens: int
    source: str
    updated_at_ms: int
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_reasoning: int | None = None
    tokens_cache_read: int | None = None
    tokens_cache_write: int | None = None
    cost: float | None = None


@dataclass
class CoverageItem:
    agent: str
    scoped_sessions: int
    matched_records: int
    supported: bool
    detail: str


def main() -> int:
    args = parse_args()
    start_ms, end_ms = resolve_range(args)
    try:
        scoped_sessions = load_scoped_sessions(start_ms, end_ms, args.agents)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    records: list[UsageRecord] = []
    coverage: list[CoverageItem] = []
    scoped_by_agent = group_scoped_by_agent(scoped_sessions)

    for agent, sessions in sorted(scoped_by_agent.items()):
        if agent == "codex":
            agent_records = analyze_codex(sessions, start_ms, end_ms)
            coverage.append(build_coverage(agent, sessions, agent_records, CODEX_DB))
            records.extend(agent_records)
        elif agent == "opencode":
            agent_records = analyze_opencode(sessions, start_ms, end_ms)
            coverage.append(build_coverage(agent, sessions, agent_records, OPENCODE_DB))
            records.extend(agent_records)
        elif agent == "omp":
            agent_records = analyze_omp(sessions, start_ms, end_ms)
            coverage.append(
                CoverageItem(
                    agent=agent,
                    scoped_sessions=len(sessions),
                    matched_records=len(agent_records),
                    supported=True,
                    detail=f"Parsed per-session JSONL under {OMP_ROOT}.",
                )
            )
            records.extend(agent_records)
        elif agent == "copilot":
            agent_records = analyze_copilot(sessions, start_ms, end_ms)
            coverage.append(
                CoverageItem(
                    agent=agent,
                    scoped_sessions=len(sessions),
                    matched_records=len(agent_records),
                    supported=True,
                    detail=f"Parsed session-state events under {COPILOT_ROOT}.",
                )
            )
            records.extend(agent_records)
        else:
            coverage.append(
                CoverageItem(
                    agent=agent,
                    scoped_sessions=len(sessions),
                    matched_records=0,
                    supported=False,
                    detail="No strict token source implemented for this agent yet.",
                )
            )

    if args.model_filter:
        needle = args.model_filter.strip().lower()
        records = [record for record in records if needle in record.model.lower()]

    report = build_report(records, coverage, start_ms, end_ms, args.limit)
    text_output = render_text_report(report)
    print(text_output)

    if args.markdown:
        Path(args.markdown).write_text(render_markdown_report(report), encoding="utf-8")
    if args.html:
        Path(args.html).write_text(render_html_report(report), encoding="utf-8")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="omux agent-report",
        description="Generate a token-usage report for agent sessions OpenMUX already knows about.",
    )
    parser.add_argument("--from", dest="from_date", help="Start date in YYYY-MM-DD format.")
    parser.add_argument("--to", dest="to_date", help="End date in YYYY-MM-DD format.")
    parser.add_argument("--markdown", help="Write the report as Markdown to this path.")
    parser.add_argument("--html", help="Write the report as a self-contained HTML file to this path.")
    parser.add_argument(
        "--agent",
        dest="agents",
        action="append",
        default=[],
        help="Filter to one or more agent names. Repeat or pass a comma-separated list.",
    )
    parser.add_argument("--model", dest="model_filter", help="Filter output rows by model substring.")
    parser.add_argument("--limit", type=int, default=10, help="Row limit per section. Default: 10.")
    return parser.parse_args()


def resolve_range(args: argparse.Namespace) -> tuple[int, int]:
    now = dt.datetime.now().astimezone()
    if args.from_date:
        start = parse_date(args.from_date, end_of_day=False)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if args.to_date:
        end = parse_date(args.to_date, end_of_day=True)
    else:
        end = now
    if start > end:
        raise SystemExit("--from must be on or before --to")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def parse_date(value: str, *, end_of_day: bool) -> dt.datetime:
    date_value = dt.date.fromisoformat(value)
    time_value = dt.time(23, 59, 59, 999000) if end_of_day else dt.time(0, 0, 0, 0)
    return dt.datetime.combine(date_value, time_value).astimezone()


def normalize_agent_filters(raw_filters: list[str]) -> set[str]:
    values: set[str] = set()
    for item in raw_filters:
        for token in item.split(","):
            token = token.strip().lower()
            if token:
                values.add(token)
    return values


def load_scoped_sessions(start_ms: int, end_ms: int, raw_filters: list[str]) -> list[ScopedSession]:
    if not OMUX_DB.exists():
        raise FileNotFoundError(f"OpenMUX Agent Sessions index not found at {OMUX_DB}. Run `omux agent-sessions reindex` first.")

    agent_filters = normalize_agent_filters(raw_filters)
    query = """
        SELECT agent, raw_id, title, cwd, source_path, updated_at_ms
        FROM agent_sessions
        WHERE deleted = 0
          AND updated_at_ms >= ?
          AND updated_at_ms <= ?
          AND title NOT GLOB '????????-????-????-????-????????????'
        ORDER BY updated_at_ms DESC
    """
    params: list[object] = [start_ms, end_ms]
    if agent_filters:
        placeholders = ", ".join("?" for _ in agent_filters)
        query = query.replace("ORDER BY", f"AND agent IN ({placeholders}) ORDER BY")
        params.extend(sorted(agent_filters))

    with sqlite3.connect(OMUX_DB) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        ScopedSession(
            agent=str(agent),
            session_id=str(session_id),
            title=str(title or session_id),
            cwd=str(cwd) if cwd else None,
            source_path=str(source_path) if source_path else None,
            updated_at_ms=int(updated_at_ms),
        )
        for agent, session_id, title, cwd, source_path, updated_at_ms in rows
    ]


def group_scoped_by_agent(sessions: Iterable[ScopedSession]) -> dict[str, list[ScopedSession]]:
    grouped: dict[str, list[ScopedSession]] = defaultdict(list)
    for session in sessions:
        grouped[session.agent].append(session)
    return grouped


def build_coverage(agent: str, sessions: list[ScopedSession], records: list[UsageRecord], primary_path: Path) -> CoverageItem:
    detail = f"Parsed strict token data from {primary_path}." if primary_path.exists() else f"Missing primary source at {primary_path}."
    return CoverageItem(
        agent=agent,
        scoped_sessions=len(sessions),
        matched_records=len(records),
        supported=True,
        detail=detail,
    )


def analyze_codex(sessions: list[ScopedSession], start_ms: int, end_ms: int) -> list[UsageRecord]:
    if not CODEX_DB.exists() or not sessions:
        return []
    session_map = {session.session_id: session for session in sessions}
    placeholders = ", ".join("?" for _ in session_map)
    query = f"""
        SELECT
            id,
            COALESCE(NULLIF(title, ''), id) AS title,
            cwd,
            COALESCE(NULLIF(model, ''), NULLIF(model_provider, ''), 'unknown') AS model_name,
            tokens_used,
            COALESCE(updated_at_ms, updated_at * 1000) AS updated_at_ms
        FROM threads
        WHERE id IN ({placeholders})
          AND archived = 0
          AND COALESCE(updated_at_ms, updated_at * 1000) >= ?
          AND COALESCE(updated_at_ms, updated_at * 1000) <= ?
          AND tokens_used > 0
    """
    params = list(session_map.keys()) + [start_ms, end_ms]
    with sqlite3.connect(CODEX_DB) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        UsageRecord(
            agent="codex",
            session_id=str(session_id),
            title=str(title),
            cwd=str(cwd) if cwd else session_map[str(session_id)].cwd,
            model=str(model_name),
            total_tokens=int(tokens_used or 0),
            source="codex_sqlite",
            updated_at_ms=int(updated_at_ms),
        )
        for session_id, title, cwd, model_name, tokens_used, updated_at_ms in rows
        if int(tokens_used or 0) > 0
    ]


def analyze_opencode(sessions: list[ScopedSession], start_ms: int, end_ms: int) -> list[UsageRecord]:
    if not OPENCODE_DB.exists() or not sessions:
        return []
    session_map = {session.session_id: session for session in sessions}
    placeholders = ", ".join("?" for _ in session_map)
    query = f"""
        SELECT
            id,
            title,
            directory,
            model,
            tokens_input,
            tokens_output,
            tokens_reasoning,
            tokens_cache_read,
            tokens_cache_write,
            cost,
            time_updated
        FROM session
        WHERE id IN ({placeholders})
          AND time_archived IS NULL
          AND time_updated >= ?
          AND time_updated <= ?
    """
    params = list(session_map.keys()) + [start_ms, end_ms]
    with sqlite3.connect(OPENCODE_DB) as conn:
        rows = conn.execute(query, params).fetchall()
    records: list[UsageRecord] = []
    for row in rows:
        (
            session_id,
            title,
            directory,
            model_raw,
            tokens_input,
            tokens_output,
            tokens_reasoning,
            tokens_cache_read,
            tokens_cache_write,
            cost,
            updated_at_ms,
        ) = row
        model_name = decode_model_value(model_raw)
        total = sum(int(value or 0) for value in [tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write])
        if total <= 0:
            continue
        records.append(
            UsageRecord(
                agent="opencode",
                session_id=str(session_id),
                title=str(title or session_map[str(session_id)].title),
                cwd=str(directory) if directory else session_map[str(session_id)].cwd,
                model=model_name,
                total_tokens=total,
                source="opencode_sqlite",
                updated_at_ms=int(updated_at_ms),
                tokens_input=int(tokens_input or 0),
                tokens_output=int(tokens_output or 0),
                tokens_reasoning=int(tokens_reasoning or 0),
                tokens_cache_read=int(tokens_cache_read or 0),
                tokens_cache_write=int(tokens_cache_write or 0),
                cost=float(cost or 0.0),
            )
        )
    return records


def analyze_omp(sessions: list[ScopedSession], start_ms: int, end_ms: int) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    for session in sessions:
        path = resolve_omp_path(session)
        if path is None or not path.exists():
            continue
        usage_by_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        current_model = "unknown"
        latest_ms = session.updated_at_ms
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    event_ts = parse_timestamp_ms(payload.get("timestamp")) or session.updated_at_ms
                    if event_ts < start_ms or event_ts > end_ms:
                        continue
                    latest_ms = max(latest_ms, event_ts)
                    if payload.get("type") == "model_change" and payload.get("model"):
                        current_model = str(payload["model"])
                    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
                    model_name = str(message.get("model") or payload.get("model") or current_model or "unknown")
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                    total = int(usage.get("totalTokens") or 0)
                    if total <= 0:
                        continue
                    bucket = usage_by_model[model_name]
                    bucket["total_tokens"] += total
                    bucket["tokens_input"] += int(usage.get("input") or 0)
                    bucket["tokens_output"] += int(usage.get("output") or 0)
                    bucket["tokens_cache_read"] += int(usage.get("cacheRead") or 0)
                    bucket["tokens_cache_write"] += int(usage.get("cacheWrite") or 0)
                    cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
                    bucket["cost"] += float(cost.get("total") or 0.0)
        except OSError:
            continue
        for model_name, totals in usage_by_model.items():
            total_tokens = int(totals["total_tokens"])
            if total_tokens <= 0:
                continue
            records.append(
                UsageRecord(
                    agent="omp",
                    session_id=session.session_id,
                    title=session.title,
                    cwd=session.cwd,
                    model=model_name,
                    total_tokens=total_tokens,
                    source="omp_jsonl",
                    updated_at_ms=latest_ms,
                    tokens_input=int(totals.get("tokens_input", 0)),
                    tokens_output=int(totals.get("tokens_output", 0)),
                    tokens_cache_read=int(totals.get("tokens_cache_read", 0)),
                    tokens_cache_write=int(totals.get("tokens_cache_write", 0)),
                    cost=float(totals.get("cost", 0.0)),
                )
            )
    return records


def resolve_omp_path(session: ScopedSession) -> Path | None:
    if session.source_path:
        path = Path(session.source_path).expanduser()
        if path.exists():
            return path
    if not OMP_ROOT.exists():
        return None
    matches = list(OMP_ROOT.rglob(f"*{session.session_id}.jsonl"))
    return matches[0] if matches else None


def analyze_copilot(sessions: list[ScopedSession], start_ms: int, end_ms: int) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    for session in sessions:
        events_path = COPILOT_ROOT / session.session_id / "events.jsonl"
        if not events_path.exists():
            continue
        latest_model = "unknown"
        latest_ms = session.updated_at_ms
        try:
            with events_path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    event_type = payload.get("type")
                    event_ts = parse_timestamp_ms(payload.get("timestamp")) or session.updated_at_ms
                    latest_ms = max(latest_ms, event_ts)
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    if event_type == "session.model_change" and data.get("newModel"):
                        latest_model = str(data["newModel"])
                    if event_type != "session.shutdown":
                        continue
                    if event_ts < start_ms or event_ts > end_ms:
                        continue
                    metrics = data.get("modelMetrics") if isinstance(data.get("modelMetrics"), dict) else {}
                    for model_name, metric_payload in metrics.items():
                        usage = metric_payload.get("usage") if isinstance(metric_payload, dict) else {}
                        total = sum(
                            int(usage.get(key) or 0)
                            for key in [
                                "inputTokens",
                                "outputTokens",
                                "cacheReadTokens",
                                "cacheWriteTokens",
                                "reasoningTokens",
                            ]
                        )
                        if total <= 0:
                            continue
                        records.append(
                            UsageRecord(
                                agent="copilot",
                                session_id=session.session_id,
                                title=session.title,
                                cwd=session.cwd,
                                model=str(model_name or latest_model),
                                total_tokens=total,
                                source="copilot_events",
                                updated_at_ms=event_ts,
                                tokens_input=int(usage.get("inputTokens") or 0),
                                tokens_output=int(usage.get("outputTokens") or 0),
                                tokens_reasoning=int(usage.get("reasoningTokens") or 0),
                                tokens_cache_read=int(usage.get("cacheReadTokens") or 0),
                                tokens_cache_write=int(usage.get("cacheWriteTokens") or 0),
                            )
                        )
        except OSError:
            continue
    return records


def decode_model_value(raw_value: object) -> str:
    if raw_value is None:
        return "unknown"
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode("utf-8", errors="replace")
    text = str(raw_value).strip()
    if not text:
        return "unknown"
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict):
            return str(payload.get("id") or payload.get("model") or payload.get("providerID") or text)
    return text


def parse_timestamp_ms(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric < 10_000_000_000:
            numeric *= 1000
        return int(numeric)
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return int(parsed.timestamp() * 1000)
    if numeric < 10_000_000_000:
        numeric *= 1000
    return int(numeric)


def build_report(records: list[UsageRecord], coverage: list[CoverageItem], start_ms: int, end_ms: int, limit: int) -> dict[str, object]:
    total_tokens = sum(record.total_tokens for record in records)
    total_cost = sum(record.cost or 0.0 for record in records)

    by_agent: dict[str, dict[str, object]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sessions": set(), "models": set()})
    by_model: dict[tuple[str, str], dict[str, object]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "sessions": set()})
    by_session: dict[tuple[str, str], dict[str, object]] = defaultdict(lambda: {"tokens": 0, "cost": 0.0, "title": "", "cwd": None, "models": set(), "updated_at_ms": 0})

    for record in records:
        agent_bucket = by_agent[record.agent]
        agent_bucket["tokens"] = int(agent_bucket["tokens"]) + record.total_tokens
        agent_bucket["cost"] = float(agent_bucket["cost"]) + float(record.cost or 0.0)
        agent_bucket["sessions"].add(record.session_id)
        agent_bucket["models"].add(record.model)

        model_bucket = by_model[(record.agent, record.model)]
        model_bucket["tokens"] = int(model_bucket["tokens"]) + record.total_tokens
        model_bucket["cost"] = float(model_bucket["cost"]) + float(record.cost or 0.0)
        model_bucket["sessions"].add(record.session_id)

        session_bucket = by_session[(record.agent, record.session_id)]
        session_bucket["tokens"] = int(session_bucket["tokens"]) + record.total_tokens
        session_bucket["cost"] = float(session_bucket["cost"]) + float(record.cost or 0.0)
        session_bucket["title"] = record.title
        session_bucket["cwd"] = record.cwd
        session_bucket["updated_at_ms"] = max(int(session_bucket["updated_at_ms"]), record.updated_at_ms)
        session_bucket["models"].add(record.model)

    top_agents = sorted(
        [
            {
                "agent": agent,
                "tokens": int(bucket["tokens"]),
                "cost": round(float(bucket["cost"]), 6),
                "sessions": len(bucket["sessions"]),
                "models": len(bucket["models"]),
            }
            for agent, bucket in by_agent.items()
        ],
        key=lambda item: (-int(item["tokens"]), str(item["agent"])),
    )[:limit]

    top_models = sorted(
        [
            {
                "agent": agent,
                "model": model,
                "tokens": int(bucket["tokens"]),
                "cost": round(float(bucket["cost"]), 6),
                "sessions": len(bucket["sessions"]),
            }
            for (agent, model), bucket in by_model.items()
        ],
        key=lambda item: (-int(item["tokens"]), str(item["agent"]), str(item["model"])),
    )[:limit]

    top_sessions = sorted(
        [
            {
                "agent": agent,
                "session_id": session_id,
                "title": str(bucket["title"]),
                "cwd": bucket["cwd"],
                "tokens": int(bucket["tokens"]),
                "cost": round(float(bucket["cost"]), 6),
                "models": ", ".join(sorted(bucket["models"]))[:100],
                "updated_at": format_datetime(int(bucket["updated_at_ms"])),
            }
            for (agent, session_id), bucket in by_session.items()
        ],
        key=lambda item: (-int(item["tokens"]), str(item["agent"]), str(item["session_id"])),
    )[:limit]

    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "range": {
            "from": format_datetime(start_ms),
            "to": format_datetime(end_ms),
            "from_ms": start_ms,
            "to_ms": end_ms,
        },
        "summary": {
            "records": len(records),
            "agents": len(by_agent),
            "tokens": total_tokens,
            "cost": round(total_cost, 6),
        },
        "top_agents": top_agents,
        "top_models": top_models,
        "top_sessions": top_sessions,
        "coverage": [
            {
                "agent": item.agent,
                "scoped_sessions": item.scoped_sessions,
                "matched_records": item.matched_records,
                "supported": item.supported,
                "detail": item.detail,
            }
            for item in sorted(coverage, key=lambda entry: entry.agent)
        ],
    }


def render_text_report(report: dict[str, object]) -> str:
    lines: list[str] = []
    summary = report["summary"]
    range_info = report["range"]
    lines.append("Agent Usage Report")
    lines.append(f"Window: {range_info['from']} -> {range_info['to']}")
    lines.append(
        f"Records: {summary['records']}  Agents: {summary['agents']}  Tokens: {human_int(int(summary['tokens']))}  Cost: {format_cost(float(summary['cost']))}"
    )
    lines.append("")
    lines.append("Top Agents")
    lines.append(
        format_table(
            ["Agent", "Tokens", "Sessions", "Models", "Cost"],
            [
                [row["agent"], human_int(int(row["tokens"])), str(row["sessions"]), str(row["models"]), format_cost(float(row["cost"]))]
                for row in report["top_agents"]
            ],
        )
    )
    lines.append("")
    lines.append("Top Models")
    lines.append(
        format_table(
            ["Agent", "Model", "Tokens", "Sessions", "Cost"],
            [
                [row["agent"], row["model"], human_int(int(row["tokens"])), str(row["sessions"]), format_cost(float(row["cost"]))]
                for row in report["top_models"]
            ],
        )
    )
    lines.append("")
    lines.append("Top Sessions")
    lines.append(
        format_table(
            ["Agent", "Session", "Tokens", "Models", "Updated", "Title"],
            [
                [
                    row["agent"],
                    row["session_id"],
                    human_int(int(row["tokens"])),
                    row["models"],
                    row["updated_at"],
                    shorten(str(row["title"]), 48),
                ]
                for row in report["top_sessions"]
            ],
        )
    )
    lines.append("")
    lines.append("Coverage")
    lines.append(
        format_table(
            ["Agent", "Scoped", "Records", "Status", "Detail"],
            [
                [
                    row["agent"],
                    str(row["scoped_sessions"]),
                    str(row["matched_records"]),
                    "strict" if row["supported"] else "unsupported",
                    row["detail"],
                ]
                for row in report["coverage"]
            ],
        )
    )
    return "\n".join(lines)


def render_markdown_report(report: dict[str, object]) -> str:
    summary = report["summary"]
    range_info = report["range"]
    parts = [
        "# Agent Usage Report",
        "",
        f"- Window: `{range_info['from']}` -> `{range_info['to']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Records: `{summary['records']}`",
        f"- Agents ranked: `{summary['agents']}`",
        f"- Total strict tokens: `{human_int(int(summary['tokens']))}`",
        f"- Total known cost: `{format_cost(float(summary['cost']))}`",
        "",
        "## Top Agents",
        markdown_table(
            ["Agent", "Tokens", "Sessions", "Models", "Cost"],
            [[row["agent"], human_int(int(row["tokens"])), row["sessions"], row["models"], format_cost(float(row["cost"]))] for row in report["top_agents"]],
        ),
        "",
        "## Top Models",
        markdown_table(
            ["Agent", "Model", "Tokens", "Sessions", "Cost"],
            [[row["agent"], row["model"], human_int(int(row["tokens"])), row["sessions"], format_cost(float(row["cost"]))] for row in report["top_models"]],
        ),
        "",
        "## Top Sessions",
        markdown_table(
            ["Agent", "Session", "Tokens", "Models", "Updated", "Title"],
            [[row["agent"], row["session_id"], human_int(int(row["tokens"])), row["models"], row["updated_at"], row["title"]] for row in report["top_sessions"]],
        ),
        "",
        "## Coverage",
        markdown_table(
            ["Agent", "Scoped", "Records", "Status", "Detail"],
            [[row["agent"], row["scoped_sessions"], row["matched_records"], "strict" if row["supported"] else "unsupported", row["detail"]] for row in report["coverage"]],
        ),
        "",
        "## Method",
        "",
        "This report only ranks sessions and models where total tokens can be established directly from a native source. Agents that OpenMUX indexed but that do not yet expose strict token totals stay visible in the coverage section and are excluded from token leaderboards.",
        "",
    ]
    return "\n".join(parts)


def render_html_report(report: dict[str, object]) -> str:
    payload = json.dumps(report, ensure_ascii=False)
    template = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Usage Report</title>
<style>
:root {{
  --bg: #f4efe6;
  --panel: #fffaf1;
  --ink: #1b1b1b;
  --muted: #5b574f;
  --line: #d9cfbe;
  --accent: #0d6b5c;
  --accent-soft: #d7efe9;
  --warn: #8b3a2b;
  --font: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Palatino, serif;
  --mono: "SFMono-Regular", Menlo, Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background:
    radial-gradient(circle at top right, rgba(13, 107, 92, 0.12), transparent 28%),
    linear-gradient(180deg, #f8f4ec 0%, var(--bg) 100%);
  color: var(--ink);
  font-family: var(--font);
}}
main {{ max-width: 1240px; margin: 0 auto; padding: 32px 20px 56px; }}
h1, h2 {{ margin: 0 0 12px; font-weight: 700; }}
p, li {{ color: var(--muted); }}
.hero {{
  display: grid;
  gap: 14px;
  padding: 22px 24px;
  border: 1px solid var(--line);
  border-radius: 24px;
  background: rgba(255, 250, 241, 0.88);
  backdrop-filter: blur(10px);
}}
.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
  margin-top: 18px;
}}
.stat {{
  padding: 14px 16px;
  border-radius: 18px;
  background: var(--panel);
  border: 1px solid var(--line);
}}
.stat-label {{ display: block; font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); }}
.stat-value {{ display: block; margin-top: 6px; font-size: 28px; color: var(--accent); }}
.section {{ margin-top: 28px; }}
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 18px;
}}
.panel {{
  border: 1px solid var(--line);
  border-radius: 22px;
  padding: 18px;
  background: rgba(255, 250, 241, 0.92);
}}
.chart {{
  display: grid;
  gap: 10px;
  margin-top: 10px;
}}
.bar-row {{
  display: grid;
  grid-template-columns: 110px 1fr auto;
  gap: 10px;
  align-items: center;
  font-size: 14px;
}}
.bar-track {{
  position: relative;
  height: 16px;
  border-radius: 999px;
  background: #ece4d7;
  overflow: hidden;
}}
.bar-fill {{
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--accent), #31a88f);
}}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ padding: 10px 8px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
th {{
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
  cursor: pointer;
}}
th[data-sort]::after {{ content: " ⇅"; color: #8b8579; }}
code {{ font-family: var(--mono); font-size: 12px; }}
.ok {{ color: var(--accent); }}
.warn {{ color: var(--warn); }}
@media (max-width: 720px) {{
  .bar-row {{ grid-template-columns: 1fr; }}
}}
</style>
<main>
  <section class="hero">
    <div>
      <h1>Agent Usage Report</h1>
      <p id="window"></p>
      <p>This report ranks only sessions with direct token evidence from native agent stores that OpenMUX already indexed.</p>
    </div>
    <div class="stats" id="stats"></div>
  </section>

  <section class="section grid">
    <div class="panel">
      <h2>Top Agents</h2>
      <div class="chart" id="agent-chart"></div>
    </div>
    <div class="panel">
      <h2>Top Models</h2>
      <div class="chart" id="model-chart"></div>
    </div>
  </section>

  <section class="section panel">
    <h2>Agent Ranking</h2>
    <table id="agents-table"></table>
  </section>

  <section class="section panel">
    <h2>Model Ranking</h2>
    <table id="models-table"></table>
  </section>

  <section class="section panel">
    <h2>Session Ranking</h2>
    <table id="sessions-table"></table>
  </section>

  <section class="section panel">
    <h2>Coverage</h2>
    <table id="coverage-table"></table>
  </section>
</main>
<script>
const report = __PAYLOAD__;

const humanInt = value => new Intl.NumberFormat("en-US").format(value);
const money = value => "$" + Number(value).toFixed(4);

function stat(label, value) {{
  return `<div class="stat"><span class="stat-label">${{label}}</span><span class="stat-value">${{value}}</span></div>`;
}}

function renderStats() {{
  const summary = report.summary;
  document.getElementById("window").textContent = `Window: ${{report.range.from}} -> ${{report.range.to}}`;
  document.getElementById("stats").innerHTML = [
    stat("Strict Tokens", humanInt(summary.tokens)),
    stat("Known Cost", money(summary.cost)),
    stat("Usage Records", humanInt(summary.records)),
    stat("Ranked Agents", humanInt(summary.agents)),
  ].join("");
}}

function renderChart(targetId, rows, labelKey) {{
  const max = Math.max(...rows.map(row => row.tokens), 1);
  document.getElementById(targetId).innerHTML = rows.map(row => {{
    const width = Math.max(4, Math.round((row.tokens / max) * 100));
    return `
      <div class="bar-row">
        <strong>${{escapeHtml(row[labelKey])}}</strong>
        <div class="bar-track"><div class="bar-fill" style="width:${{width}}%"></div></div>
        <span>${{humanInt(row.tokens)}}</span>
      </div>
    `;
  }}).join("");
}}

function escapeHtml(value) {{
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}}

function buildTable(targetId, columns, rows) {{
  const table = document.getElementById(targetId);
  table.innerHTML = "";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach((column, index) => {{
    const th = document.createElement("th");
    th.textContent = column.label;
    th.dataset.sort = column.type || "text";
    th.addEventListener("click", () => sortTable(table, index, column.type || "text"));
    headRow.appendChild(th);
  }});
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  rows.forEach(row => {{
    const tr = document.createElement("tr");
    columns.forEach(column => {{
      const td = document.createElement("td");
      td.innerHTML = column.render ? column.render(row[column.key], row) : escapeHtml(row[column.key]);
      tr.appendChild(td);
    }});
    tbody.appendChild(tr);
  }});
  table.appendChild(tbody);
}}

function sortTable(table, index, type) {{
  const tbody = table.querySelector("tbody");
  const current = table.dataset.sortDir === "asc" ? "asc" : "desc";
  const next = current === "asc" ? "desc" : "asc";
  const rows = [...tbody.querySelectorAll("tr")];
  rows.sort((a, b) => {{
    const av = a.children[index].textContent.trim();
    const bv = b.children[index].textContent.trim();
    const left = type === "number" ? Number(av.replaceAll(/[^0-9.-]/g, "")) : av.toLowerCase();
    const right = type === "number" ? Number(bv.replaceAll(/[^0-9.-]/g, "")) : bv.toLowerCase();
    if (left < right) return next === "asc" ? -1 : 1;
    if (left > right) return next === "asc" ? 1 : -1;
    return 0;
  }});
  tbody.replaceChildren(...rows);
  table.dataset.sortDir = next;
}}

function renderTables() {{
  buildTable("agents-table", [
    {{ key: "agent", label: "Agent" }},
    {{ key: "tokens", label: "Tokens", type: "number", render: value => humanInt(value) }},
    {{ key: "sessions", label: "Sessions", type: "number" }},
    {{ key: "models", label: "Models", type: "number" }},
    {{ key: "cost", label: "Cost", type: "number", render: value => money(value) }},
  ], report.top_agents);

  buildTable("models-table", [
    {{ key: "agent", label: "Agent" }},
    {{ key: "model", label: "Model" }},
    {{ key: "tokens", label: "Tokens", type: "number", render: value => humanInt(value) }},
    {{ key: "sessions", label: "Sessions", type: "number" }},
    {{ key: "cost", label: "Cost", type: "number", render: value => money(value) }},
  ], report.top_models);

  buildTable("sessions-table", [
    {{ key: "agent", label: "Agent" }},
    {{ key: "session_id", label: "Session" }},
    {{ key: "tokens", label: "Tokens", type: "number", render: value => humanInt(value) }},
    {{ key: "models", label: "Models" }},
    {{ key: "updated_at", label: "Updated" }},
    {{ key: "title", label: "Title" }},
  ], report.top_sessions);

  buildTable("coverage-table", [
    {{ key: "agent", label: "Agent" }},
    {{ key: "scoped_sessions", label: "Scoped", type: "number" }},
    {{ key: "matched_records", label: "Records", type: "number" }},
    {{ key: "supported", label: "Status", render: value => value ? '<span class="ok">strict</span>' : '<span class="warn">unsupported</span>' }},
    {{ key: "detail", label: "Detail" }},
  ], report.coverage);
}

renderStats();
renderChart("agent-chart", report.top_agents, "agent");
renderChart("model-chart", report.top_models.map(row => ({{ ...row, model: row.model.split('/').pop() }})), "model");
renderTables();
</script>
</html>
"""
    template = template.replace("{{", "{").replace("}}", "}")
    return template.replace("__PAYLOAD__", payload)


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(no rows)"
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
    separator = "  ".join("-" * width for width in widths)
    return "\n".join([fmt(headers), separator] + [fmt(row) for row in rows])


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    rendered_rows = [[str(value) for value in row] for row in rows] or [["-", "-", "-", "-", "-"][: len(headers)]]
    separator = ["---"] * len(headers)
    parts = ["| " + " | ".join(headers) + " |", "| " + " | ".join(separator) + " |"]
    for row in rendered_rows:
        parts.append("| " + " | ".join(row) + " |")
    return "\n".join(parts)


def human_int(value: int) -> str:
    return f"{value:,}"


def format_cost(value: float) -> str:
    return f"${value:.4f}"


def format_datetime(value_ms: int) -> str:
    return dt.datetime.fromtimestamp(value_ms / 1000, tz=dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
