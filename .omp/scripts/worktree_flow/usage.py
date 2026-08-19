"""Privacy-preserving OMP usage collection with bounded JSONL readers."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping
from .models import FlowError, SessionSnapshot, UsageSource, UsageTotals
from .paths import (
    MAX_JSONL_LINE_BYTES,
    atomic_write_json,
    ensure_directory,
    iter_bounded_jsonl,
    lstat_regular,
    optional_regular_file,
    reject_symlink_components,
)
JsonObject = dict[str, Any]
Number = int | float


@dataclass
class UsageAggregate:
    totals: dict[str, Number]
    nested_response_usage: dict[str, Number]
    models: dict[str, JsonObject]
    tools: dict[str, dict[str, Number]]
    context: dict[str, Number]
    timings: dict[str, Number]
    event_counts: dict[str, int]

    @classmethod
    def empty(cls) -> "UsageAggregate":
        return cls(
            totals=asdict(UsageTotals()),
            nested_response_usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            models={},
            tools={},
            context={
                "max_prompt_tokens": 0,
                "last_prompt_tokens": 0,
                "max_non_message_tokens": 0,
                "last_non_message_tokens": 0,
            },
            timings={"message_duration_ms": 0, "message_ttft_ms": 0, "message_count": 0},
            event_counts={},
        )

    def as_payload(self) -> JsonObject:
        return {
            "totals": self.totals,
            "nested_response_usage": self.nested_response_usage,
            "models": self.models,
            "tools": self.tools,
            "context": self.context,
            "timings": self.timings,
            "event_counts": self.event_counts,
        }


class UsageCollector:
    def __init__(self, *, harness: str, harness_dir: Path, dry_run: bool = False) -> None:
        self.harness = harness
        self.harness_dir = harness_dir
        self.dry_run = dry_run
        self._run_id: str | None = None

    def set_run_id(self, run_id: str | None) -> None:
        self._run_id = run_id

    @property
    def is_omp(self) -> bool:
        name = Path(self.harness).name.lower()
        return name.removesuffix(".exe") == "omp"

    def files(self, worktree: Path) -> tuple[Path, Path, Path]:
        root = worktree / self.harness_dir / "handoff"
        return (
            root / "usage-events.jsonl",
            root / "usage-summary.json",
            root / "usage-sources.json",
        )

    def snapshot_sessions(self, repo: Path) -> SessionSnapshot:
        return SessionSnapshot({str(path.resolve()): self.safe_mtime_ns(path) for path in self.newest_session_files(repo)})

    def wsl_windows_home_from_repo(self, repo: Path) -> Path | None:
        match = re.match(r"^/mnt/([A-Za-z])/Users/([^/]+)(?:/|$)", repo.resolve().as_posix())
        if match is None:
            return None
        drive, user = match.groups()
        return Path("/mnt") / drive.lower() / "Users" / user

    def session_roots(self, repo: Path) -> list[Path]:
        roots = [Path.home() / ".omp" / "agent" / "sessions"]
        windows_home = self.wsl_windows_home_from_repo(repo)
        if windows_home is not None:
            roots.append(windows_home / ".omp" / "agent" / "sessions")
        valid_roots: list[Path] = []
        for root in dict.fromkeys(roots):
            try:
                reject_symlink_components(root, include_final=True)
                info = root.lstat()
            except (OSError, FlowError):
                continue
            if stat.S_ISDIR(info.st_mode):
                valid_roots.append(root)
        return valid_roots

    def newest_session_files(self, repo: Path) -> list[Path]:
        files: list[tuple[int, Path]] = []
        for root in self.session_roots(repo):
            try:
                for path in root.rglob("*.jsonl"):
                    try:
                        lstat_regular(path, label="session log")
                    except FlowError:
                        continue
                    files.append((self.safe_mtime_ns(path), path))
            except OSError:
                continue
        files.sort(key=lambda item: item[0], reverse=True)
        return [path for _mtime, path in files[:2000]]

    @staticmethod
    def safe_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    def changed_session_files(self, repo: Path, snapshot: SessionSnapshot) -> list[Path]:
        changed: list[tuple[int, Path]] = []
        for path in self.newest_session_files(repo):
            resolved = str(path.resolve())
            mtime = self.safe_mtime_ns(path)
            if snapshot.files.get(resolved) is None or mtime > snapshot.files[resolved]:
                changed.append((mtime, path))
        changed.sort(key=lambda item: item[0], reverse=True)
        return [path for _mtime, path in changed[:2000]]

    @staticmethod
    def _reject_json_constant(value: str) -> object:
        raise ValueError(f"non-standard JSON constant: {value}")

    def read_jsonl_records(self, path: Path) -> Iterator[JsonObject]:
        for raw in iter_bounded_jsonl(path, max_line_bytes=MAX_JSONL_LINE_BYTES):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"), parse_constant=self._reject_json_constant)
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(value, dict):
                yield value

    def session_cwd(self, path: Path) -> str | None:
        for record in self.read_jsonl_records(path):
            if record.get("type") not in {"session", "session_init"}:
                continue
            cwd = self.string_at(record, ("cwd",)) or self.string_at(record, ("data", "cwd"))
            if cwd:
                return cwd
        return None

    @staticmethod
    def casefold_path_text(value: str) -> str:
        return value.lower() if re.match(r"^[A-Za-z]:/", value) else value

    def path_matches_worktree(self, raw_cwd: str, worktree: Path) -> bool:
        raw = raw_cwd.replace("\\", "/").rstrip("/")
        candidates = {raw}
        try:
            candidates.add(Path(raw_cwd).expanduser().resolve().as_posix().rstrip("/"))
        except (OSError, RuntimeError):
            pass
        resolved = worktree.resolve().as_posix().rstrip("/")
        expected = {worktree.as_posix().rstrip("/"), resolved}
        windows = self.wsl_drive_mount_to_windows_path(worktree.resolve())
        if windows:
            expected.add(windows.replace("\\", "/").rstrip("/"))
        return bool(
            {self.casefold_path_text(item) for item in candidates}
            & {self.casefold_path_text(item) for item in expected}
        )

    @staticmethod
    def wsl_drive_mount_to_windows_path(path: Path) -> str | None:
        match = re.fullmatch(r"/mnt/([A-Za-z])(?:/(.*))?", path.as_posix())
        if match is None:
            return None
        drive, tail = match.groups()
        return f"{drive.upper()}:\\{tail.replace('/', '\\') if tail else ''}".rstrip("\\")

    def _number(self, value: Any) -> Number | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    def numeric_at(self, data: Mapping[str, Any], path: tuple[str, ...]) -> Number | None:
        value: Any = data
        for key in path:
            if not isinstance(value, Mapping):
                return None
            value = value.get(key)
        return self._number(value)

    @staticmethod
    def string_at(data: Mapping[str, Any], path: tuple[str, ...]) -> str | None:
        value: Any = data
        for key in path:
            if not isinstance(value, Mapping):
                return None
            value = value.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def mapping_at(data: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any] | None:
        value: Any = data
        for key in path:
            if not isinstance(value, Mapping):
                return None
            value = value.get(key)
        return value if isinstance(value, Mapping) else None

    @staticmethod
    def increment(target: MutableMapping[str, Any], key: str, amount: Number = 1) -> None:
        value = target.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = value + amount
        else:
            target[key] = amount

    def collect_config(self, record: JsonObject, aggregate: UsageAggregate) -> bool:
        contributed = False
        for record_type, path, metric in (
            ("model_change", ("model_change", "model"), "config_selections"),
            ("thinking_level_change", ("thinking_level_change", "thinkingLevel"), "thinking_level_selections"),
            ("service_tier_change", ("service_tier_change", "serviceTier"), "service_tier_selections"),
        ):
            if record.get("type") != record_type:
                continue
            value = self.string_at(record, path) or self.string_at(record, ("data", path[-1]))
            if value:
                bucket = aggregate.models.setdefault(value, {})
                self.increment(bucket, metric)
                contributed = True
        return contributed

    def collect_assistant(self, message: JsonObject, aggregate: UsageAggregate) -> bool:
        if message.get("role") != "assistant":
            return False
        contributed = False
        usage = message.get("usage")
        if isinstance(usage, Mapping):
            for source, destination in {
                "input": "input_tokens",
                "output": "output_tokens",
                "cacheRead": "cache_read_tokens",
                "cacheWrite": "cache_write_tokens",
                "reasoningTokens": "reasoning_tokens",
                "totalTokens": "total_tokens",
            }.items():
                value = self.numeric_at(usage, (source,))
                if value is not None:
                    self.increment(aggregate.totals, destination, int(value))
                    contributed = True
            cost = usage.get("cost")
            if isinstance(cost, Mapping):
                for source, destination in {
                    "input": "cost_input",
                    "output": "cost_output",
                    "cacheRead": "cost_cache_read",
                    "cacheWrite": "cost_cache_write",
                    "total": "cost_total",
                }.items():
                    value = self.numeric_at(cost, (source,))
                    if value is not None:
                        self.increment(aggregate.totals, destination, float(value))
                        contributed = True
        model = self.string_at(message, ("model",))
        if model:
            bucket = aggregate.models.setdefault(model, {})
            self.increment(bucket, "assistant_messages")
            for source, destination in (("provider", "providers"), ("api", "apis"), ("stopReason", "stop_reasons")):
                value = self.string_at(message, (source,))
                if value:
                    nested = bucket.setdefault(destination, {})
                    if isinstance(nested, dict):
                        self.increment(nested, value)
            contributed = True
        duration = self.numeric_at(message, ("duration",))
        if duration is not None:
            self.increment(aggregate.timings, "message_duration_ms", int(float(duration) * 1000))
            self.increment(aggregate.timings, "message_count")
            contributed = True
        ttft = self.numeric_at(message, ("ttft",))
        if ttft is not None:
            self.increment(aggregate.timings, "message_ttft_ms", int(float(ttft) * 1000))
            contributed = True
        return contributed

    def collect_context(self, message: JsonObject, aggregate: UsageAggregate) -> bool:
        snapshot = message.get("contextSnapshot")
        if not isinstance(snapshot, Mapping):
            return False
        contributed = False
        for source, last_key, max_key in (
            ("promptTokens", "last_prompt_tokens", "max_prompt_tokens"),
            ("nonMessageTokens", "last_non_message_tokens", "max_non_message_tokens"),
        ):
            value = self.numeric_at(snapshot, (source,))
            if value is not None:
                aggregate.context[last_key] = int(value)
                aggregate.context[max_key] = max(int(aggregate.context.get(max_key, 0)), int(value))
                contributed = True
        return contributed

    def collect_nested(self, message: JsonObject, aggregate: UsageAggregate) -> bool:
        usage = self.mapping_at(message, ("details", "response", "usage"))
        if usage is None:
            return False
        contributed = False
        for source, destination in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"), ("totalTokens", "total_tokens")):
            value = self.numeric_at(usage, (source,))
            if value is not None:
                self.increment(aggregate.nested_response_usage, destination, int(value))
                contributed = True
        return contributed

    def collect_tool(self, record: JsonObject, aggregate: UsageAggregate) -> bool:
        if record.get("customType") == "tool_execution_start":
            tool_name = self.string_at(record, ("data", "toolName"))
            if tool_name:
                bucket = aggregate.tools.setdefault(tool_name, {"calls": 0, "results": 0, "errors": 0})
                self.increment(bucket, "calls")
                return True
        message = record.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "toolResult":
            return False
        tool_name = message.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            return False
        bucket = aggregate.tools.setdefault(tool_name, {"calls": 0, "results": 0, "errors": 0})
        self.increment(bucket, "results")
        if message.get("isError") is True:
            self.increment(bucket, "errors")
        details = message.get("details")
        if isinstance(details, Mapping):
            for source, destination in (("wallTimeMs", "wall_time_ms"), ("exitCode", "exit_code_total"), ("timeoutSeconds", "timeout_seconds"), ("fileCount", "file_count"), ("matchCount", "match_count")):
                value = self.numeric_at(details, (source,))
                if value is not None:
                    self.increment(bucket, destination, int(value))
            for source, destination in (("fileLimitReached", "file_limit_reached"), ("resultLimitReached", "result_limit_reached")):
                if details.get(source) is True:
                    self.increment(bucket, destination)
        return True

    def collect_session_usage(self, path: Path, source_id: str) -> tuple[UsageAggregate, UsageSource]:
        aggregate = UsageAggregate.empty()
        event_counts: dict[str, int] = {}
        record_ids: list[str] = []
        session_id: str | None = None
        records_read = 0
        for records_read, record in enumerate(self.read_jsonl_records(path), start=1):
            session_id = session_id or self.session_id_from_record(record)
            for key in ("type", "customType"):
                value = record.get(key)
                if isinstance(value, str) and value:
                    self.increment(event_counts, value)
                    self.increment(aggregate.event_counts, value)
            contributed = self.collect_config(record, aggregate)
            message = record.get("message")
            if isinstance(message, dict):
                contributed = self.collect_assistant(message, aggregate) or contributed
                contributed = self.collect_context(message, aggregate) or contributed
                contributed = self.collect_nested(message, aggregate) or contributed
                contributed = self.collect_tool(record, aggregate) or contributed
            else:
                contributed = self.collect_tool(record, aggregate) or contributed
            if contributed and len(record_ids) < 50:
                record_id = self.safe_record_id(record)
                if record_id:
                    record_ids.append(record_id)
        path_hash = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
        return aggregate, UsageSource(source_id, session_id, path.name, path_hash, records_read, event_counts, record_ids)

    @staticmethod
    def session_id_from_record(record: Mapping[str, Any]) -> str | None:
        for key in ("session_id", "sessionId", "sessionID", "id"):
            value = record.get(key)
            if isinstance(value, str) and value:
                return value
        data = record.get("data")
        if isinstance(data, Mapping):
            for key in ("session_id", "sessionId", "sessionID", "id"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def safe_record_id(record: Mapping[str, Any]) -> str | None:
        for key in ("id", "messageId", "recordId"):
            value = record.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
                return value
        message = record.get("message")
        if isinstance(message, Mapping):
            value = message.get("id")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
                return value
        return None

    def base_event(self, phase: str, worktree: Path, result: Any) -> JsonObject:
        return {
            "schema_version": 1,
            "timestamp": __import__("datetime").datetime.now().isoformat(timespec="milliseconds"),
            "run_id": self._run_id,
            "harness": self.harness,
            "harness_dir": self.harness_dir.as_posix(),
            "phase": phase,
            "status": "collected",
            "command_returncode": result.returncode,
            "command_timed_out": result.timed_out,
            "command_duration_ms": result.duration_ms,
            "command_started_at": result.started_at,
            "command_finished_at": result.finished_at,
            "command_stdout_bytes": result.stdout_bytes,
            "command_stderr_bytes": result.stderr_bytes,
            "totals": {},
            "nested_response_usage": {},
            "models": {},
            "tools": {},
            "context": {},
            "timings": {},
            "event_counts": {},
            "sources": [],
        }

    def collect_phase_usage(self, repo: Path, worktree: Path, phase: str, snapshot: SessionSnapshot, result: Any) -> JsonObject:
        event = self.base_event(phase, worktree, result)
        if not self.is_omp:
            event.update({"status": "unavailable", "reason": "non_omp_harness"})
            return event
        selected = [path for path in self.changed_session_files(repo, snapshot) if (cwd := self.session_cwd(path)) and self.path_matches_worktree(cwd, worktree)]
        if not selected:
            event.update({"status": "unavailable", "reason": "no_matching_session_files"})
            return event
        aggregate = UsageAggregate.empty()
        sources: list[JsonObject] = []
        for index, path in enumerate(selected, start=1):
            source_aggregate, source = self.collect_session_usage(path, f"session-{index}")
            self.merge_aggregate(aggregate, source_aggregate)
            sources.append(asdict(source))
        event.update({"status": "collected", **aggregate.as_payload(), "sources": sources})
        return event

    def merge_aggregate(self, target: UsageAggregate, source: UsageAggregate) -> None:
        self.merge_numeric(target.totals, source.totals)
        self.merge_numeric(target.nested_response_usage, source.nested_response_usage)
        self.merge_numeric(target.timings, source.timings)
        self.merge_numeric(target.event_counts, source.event_counts)
        for key in ("max_prompt_tokens", "max_non_message_tokens"):
            target.context[key] = max(int(target.context.get(key, 0)), int(source.context.get(key, 0)))
        for key in ("last_prompt_tokens", "last_non_message_tokens"):
            if source.context.get(key):
                target.context[key] = source.context[key]
        self.merge_models(target.models, source.models)
        for name, stats in source.tools.items():
            bucket = target.tools.setdefault(name, {})
            self.merge_numeric(bucket, stats)

    @staticmethod
    def merge_numeric(target: MutableMapping[str, Any], source: Mapping[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                current = target.get(key, 0)
                if isinstance(current, (int, float)) and not isinstance(current, bool):
                    target[key] = current + value
                else:
                    target[key] = value

    def merge_models(self, target: MutableMapping[str, JsonObject], source: Mapping[str, Any]) -> None:
        for model, value in source.items():
            if not isinstance(value, Mapping):
                continue
            destination = target.setdefault(model, {})
            for key, item in value.items():
                if isinstance(item, (int, float)) and not isinstance(item, bool):
                    self.increment(destination, key, item)
                elif isinstance(item, Mapping):
                    nested = destination.setdefault(key, {})
                    if isinstance(nested, dict):
                        for nested_key, nested_value in item.items():
                            if isinstance(nested_value, Mapping):
                                amount = nested_value.get("count", 0)
                            else:
                                amount = nested_value
                            if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                                bucket = nested.setdefault(nested_key, {"count": 0})
                                if isinstance(bucket, dict):
                                    self.increment(bucket, "count", amount)

    @staticmethod
    def compact(value: Mapping[str, Any]) -> JsonObject:
        output: JsonObject = {}
        for key, item in value.items():
            if isinstance(item, (int, float)) and item == 0:
                continue
            if item:
                output[key] = item
        return output

    def append_event(self, worktree: Path, event: JsonObject) -> None:
        path, _summary, _sources = self.files(worktree)
        if self.dry_run:
            print(f"+ write {path}")
            return
        ensure_directory(path.parent, mode=0o700)
        from .paths import atomic_write_bytes, read_bytes_bounded

        previous = read_bytes_bounded(path, max_bytes=10 * 1024 * 1024) if optional_regular_file(path, label="usage events") else b""
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(line) > MAX_JSONL_LINE_BYTES:
            raise FlowError("Usage event exceeds the JSONL line limit.")
        atomic_write_bytes(path, previous + line, max_bytes=10 * 1024 * 1024)

    def rewrite_artifacts(self, worktree: Path) -> None:
        events_path, summary_path, sources_path = self.files(worktree)
        events = list(self.read_jsonl_records(events_path)) if optional_regular_file(events_path, label="usage events") else []
        summary = self.build_summary(events)
        sources: list[Any] = []
        for event in events:
            value = event.get("sources")
            if isinstance(value, list):
                sources.extend(item for item in value if isinstance(item, dict))
        if self.dry_run:
            print(f"+ write {summary_path}")
            print(f"+ write {sources_path}")
            return
        atomic_write_json(summary_path, summary)
        atomic_write_json(sources_path, {"schema_version": 1, "run_id": self._run_id, "harness": self.harness, "harness_dir": self.harness_dir.as_posix(), "sources": sources})

    def build_summary(self, events: list[JsonObject]) -> JsonObject:
        phase_names = ("implementation", "audit", "conflict_resolution", "post_conflict_audit")
        phases: JsonObject = {name: {} for name in phase_names}
        totals: dict[str, Number] = {}
        nested: dict[str, Number] = {}
        models: dict[str, JsonObject] = {}
        tools: dict[str, dict[str, Number]] = {}
        path_hashes: set[str] = set()
        run_id = self._run_id
        for event in events:
            if run_id is None and isinstance(event.get("run_id"), str):
                run_id = event["run_id"]
            phase = event.get("phase")
            if not isinstance(phase, str):
                continue
            phase_summary = phases.setdefault(phase, {})
            if not isinstance(phase_summary, dict):
                continue
            self.increment(phase_summary, "runs")
            status = event.get("status")
            if isinstance(status, str):
                counts = phase_summary.setdefault("status_counts", {})
                if isinstance(counts, dict):
                    self.increment(counts, status)
            for source_key, destination in (("totals", totals), ("nested_response_usage", nested)):
                value = event.get(source_key)
                if isinstance(value, Mapping):
                    current = phase_summary.setdefault(source_key, {})
                    if isinstance(current, dict):
                        self.merge_numeric(current, value)
                    self.merge_numeric(destination, value)
            value = event.get("models")
            if isinstance(value, Mapping):
                current = phase_summary.setdefault("models", {})
                if isinstance(current, dict):
                    self.merge_models(current, value)
                self.merge_models(models, value)
            value = event.get("tools")
            if isinstance(value, Mapping):
                current = phase_summary.setdefault("tools", {})
                if isinstance(current, dict):
                    for name, stats in value.items():
                        if isinstance(stats, Mapping):
                            bucket = current.setdefault(name, {})
                            if isinstance(bucket, dict):
                                self.merge_numeric(bucket, stats)
                for name, stats in value.items():
                    if isinstance(stats, Mapping):
                        bucket = tools.setdefault(name, {})
                        self.merge_numeric(bucket, stats)
            source_values = event.get("sources")
            if isinstance(source_values, list):
                path_hashes.update(item["path_hash"] for item in source_values if isinstance(item, Mapping) and isinstance(item.get("path_hash"), str))
        for phase_summary in phases.values():
            if isinstance(phase_summary, dict):
                for key in ("totals", "nested_response_usage"):
                    value = phase_summary.get(key)
                    if isinstance(value, Mapping):
                        phase_summary[key] = self.compact(value)
        return {
            "schema_version": 1,
            "run_id": run_id,
            "harness": self.harness,
            "harness_dir": self.harness_dir.as_posix(),
            "phases": phases,
            "totals": self.compact(totals),
            "nested_response_usage": self.compact(nested),
            "models": models,
            "tools": tools,
            "sources": {"count": len(path_hashes), "path_hashes": sorted(path_hashes)},
            "privacy": {"prompt_text_logged": False, "response_text_logged": False, "tool_argument_values_logged": False, "session_paths_logged": False},
        }
