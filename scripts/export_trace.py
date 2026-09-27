#!/usr/bin/env python3
"""Export one complete capture attempt using a narrow file allowlist (stdlib only)."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

from inspect_trace import credential_findings, digest, inspect_bundle, safe_child


REQUIRED = ("trajectory.json", "task.json", "result.json", "agent-environment.json",
            "eval-environment.json", "prediction.patch", "eval.sh", "eval_report.json")
OPTIONAL = ("test_output.txt",)
ATTEMPT = re.compile(r"run/(j[0-9]+)/attempt-[A-Za-z0-9_-]+\Z")
TRACE_FILE = re.compile(r"[0-9]{6}-(?:api_request|api_response|api_billing|tool_request|tool_result)\.json\Z")
LOG_FILE = re.compile(r"(?:(?:agent|eval)-tool-[0-9]+|output[A-Za-z0-9_.-]*)\.log\Z")


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def walk(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield path + (key,), child
            yield from walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (str(index),))


def identifier_category(path):
    key = path[-1]
    if key in ("request_id", "provider_request_id"):
        return "request"
    if key == "response_id" or (key == "id" and len(path) > 1 and path[-2] == "response"):
        return "response"
    if key == "tool_call_id" or (key == "id" and "tool_calls" in path):
        return "tool"
    return None


def sensitive_metadata_path(path):
    return path[-1].lower() in {"hostname", "host_id", "container_id", "machine_id"}


def export(source_root: Path, source_attempt: str, destination: Path) -> dict:
    match = ATTEMPT.fullmatch(source_attempt)
    if not match:
        raise ValueError("--attempt must be run/jNNN/attempt-NAME")
    # An explicit relative attempt is mandatory. Never recurse over the capture root.
    source_root = source_root.resolve()
    attempt = safe_child(source_root, source_attempt)
    if not attempt.is_dir():
        raise ValueError("source attempt is not a directory")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", source_root.name):
        raise ValueError("source root needs a non-sensitive capture-group directory name")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists; choose a new directory")
    if destination.resolve().is_relative_to(source_root):
        raise ValueError("destination must be outside the source capture root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = {name: safe_child(attempt, name).read_bytes() for name in REQUIRED}
    for name in OPTIONAL:
        path = safe_child(attempt, name)
        if path.exists():
            files[name] = path.read_bytes()
    trace_dir = safe_child(attempt, "trace")
    trace_paths = sorted(trace_dir.iterdir())
    if not trace_paths or any(not TRACE_FILE.fullmatch(p.name) for p in trace_paths):
        raise ValueError("trace directory has no events or an unsupported entry")
    events = []
    for path in trace_paths:
        name = "trace/" + path.name
        files[name] = safe_child(attempt, name).read_bytes()
        event = json.loads(files[name])
        events.append(event)
        if event["type"] == "tool_result":
            log_name = event["output_file"]
            if not LOG_FILE.fullmatch(log_name):
                raise ValueError("tool output filename is outside the log allowlist")
            files[log_name] = safe_child(attempt, log_name).read_bytes()
            if digest(files[log_name]) != event["output_sha256"]:
                raise ValueError("source tool log checksum mismatch")
            if event["result"] is None and not event.get("exception_type"):
                raise ValueError("tool result has neither output nor recorded exception")
            if event["result"] is not None and files[log_name] != event["result"]["output"].encode("utf-8"):
                raise ValueError("source tool log differs from recorded output")
    findings = credential_findings(files)
    if findings:
        for finding in findings:
            print(f"{finding['category']}: {finding['path']}", file=sys.stderr)
        raise ValueError("credential-like material detected; export stopped without printing values")

    source_json = {name: json.loads(data) for name, data in files.items() if name.endswith(".json")}
    result = source_json["result.json"]
    if result["state"] != "completed" or not result.get("capture_complete"):
        raise ValueError("source capture must be completed and capture_complete")
    if result["trajectory_sha256"] != digest(files["trajectory.json"]):
        raise ValueError("source trajectory checksum mismatch")
    if result["job_id"] != match.group(1) or result["instance_id"] != source_json["task.json"]["instance_id"]:
        raise ValueError("source job or instance identity mismatch")
    target_attempt = f"run/{result['job_id']}/attempt-sample"
    original_host_attempt = result["attempt_path"]
    private_home = re.match(r"/(?:home|Users)/[^/]+", original_host_attempt)
    private_home = private_home.group(0) if private_home else None

    # Stable opaque aliases keep all request/response/tool references joinable.
    aliases = {}
    counts = Counter()
    ordered_json = [json.loads(files["trace/" + p.name]) for p in trace_paths]
    ordered_json += [source_json[name] for name in sorted(source_json) if not name.startswith("trace/")]
    for obj in ordered_json:
        for path, value in walk(obj):
            category = identifier_category(path)
            if category and isinstance(value, str) and value and value not in aliases:
                counts[category] += 1
                aliases[value] = f"sample-{category}-{counts[category]:04d}"

    transformations = {name: set() for name in files}

    def transform(obj, name, path=()):
        if isinstance(obj, dict):
            answer = {}
            for key, value in obj.items():
                child_path = path + (key,)
                category = identifier_category(child_path)
                if category and isinstance(value, str) and value:
                    answer[key] = aliases[value]
                    transformations[name].add("pseudonymize-correlated-api-and-tool-identifiers")
                elif key == "system_fingerprint" and value is not None:
                    answer[key] = None
                    transformations[name].add("remove-provider-system-fingerprint")
                elif sensitive_metadata_path(child_path) and value is not None:
                    answer[key] = None
                    transformations[name].add("remove-host-identity-metadata")
                elif key == "Pid" and "docker_state" in path:
                    answer[key] = None
                    transformations[name].add("remove-host-process-id")
                elif key == "session_id" and name == "result.json":
                    answer[key] = "sample"
                    transformations[name].add("replace-capture-session-id")
                elif key == "attempt_path" and name == "result.json":
                    answer[key] = target_attempt
                    transformations[name].add("relocate-attempt-path")
                elif child_path == ("info", "config", "agent", "output_path") and name == "trajectory.json":
                    answer[key] = target_attempt + "/trajectory.json"
                    transformations[name].add("relocate-trajectory-output-path")
                elif child_path == ("budget", "ledger_path") and name == "trajectory.json":
                    answer[key] = None
                    transformations[name].add("remove-unexported-budget-ledger-path")
                else:
                    answer[key] = transform(value, name, child_path)
            return answer
        if isinstance(obj, list):
            return [transform(child, name, path + (str(index),)) for index, child in enumerate(obj)]
        return obj

    transformed = {name: transform(copy.deepcopy(value), name) for name, value in source_json.items()}
    # Refuse to repair commands, outputs, or prompts silently: their text must match.
    protected = {"command", "arguments", "output", "content", "reasoning_content", "problem_statement"}
    for name, original in source_json.items():
        before = {path: value for path, value in walk(original) if path[-1] in protected}
        after = {path: value for path, value in walk(transformed[name]) if path[-1] in protected}
        if before != after:
            raise ValueError("protected command/output/prompt text would change")
    exported = dict(files)
    for name, value in transformed.items():
        if value != source_json[name]:
            exported[name] = encoded(value)
    transformed["result.json"]["trajectory_sha256"] = digest(exported["trajectory.json"])
    transformations["result.json"].add("recompute-exported-trajectory-sha256")
    exported["result.json"] = encoded(transformed["result.json"])
    # Host paths are only removed from known metadata. Any unexpected occurrence
    # (including in commands/output) is an explicit stop requiring human review.
    for name, data in exported.items():
        text = data.decode("utf-8", errors="replace")
        if (private_home and private_home in text) or original_host_attempt in text:
            raise ValueError(f"private-capture-path remains in {name}; review without altering replay text")
    findings = credential_findings(exported)
    if findings:
        for finding in findings:
            print(f"{finding['category']}: {finding['path']}", file=sys.stderr)
        raise ValueError("credential scan failed after transformation")

    records = []
    for name in sorted(exported):
        records.append({"path": target_attempt + "/" + name,
                        "sha256": digest(exported[name]), "size_bytes": len(exported[name]),
                        "source_path": source_attempt + "/" + name,
                        "source_sha256": digest(files[name]), "source_size_bytes": len(files[name]),
                        "transformations": sorted(transformations[name])})
    manifest = {
        "schema_version": 1, "kind": "sanitized-trace-bundle",
        "source": {"capture_group": source_root.name, "attempt_path": source_attempt,
                   "selection": "explicit canonical job/attempt from the original capture group",
                   "raw_source_attempt_included": False,
                   "source_hash_note": "Hashes identify original bytes; private host paths and raw identifiers are not published."},
        "tasks": [{"job_id": result["job_id"], "instance_id": result["instance_id"],
                   "attempt_path": target_attempt}],
        "preservation": {"tool_commands": "unchanged", "tool_outputs": "byte-for-byte unchanged",
                         "prompt_and_model_text": "unchanged", "timestamps_and_durations": "unchanged",
                         "benchmark_task": "byte-for-byte unchanged",
                         "public_benchmark_traceback_paths": "retained as original problem-statement text"},
        "sanitization": {"identifier_aliases": dict(counts), "raw_alias_map_included": False,
                         "private_host_paths": "relocated or removed only in known metadata",
                         "source_files_modified": False,
                         "credential_scan": "No configured credential patterns matched; heuristic, not a guarantee."},
        "files": records,
    }
    temporary = Path(tempfile.mkdtemp(prefix=".trace-export-", dir=destination.parent))
    try:
        for name, data in exported.items():
            target = temporary / target_attempt / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temporary / "bundle-manifest.json").write_bytes(encoded(manifest))
        summary = inspect_bundle(temporary, scan=True, command_limit=0)
        if summary["credential_scan"]["findings"]:
            raise ValueError("bundle verification credential scan failed")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path,
                        help="capture-group directory; never copied recursively")
    parser.add_argument("--attempt", required=True, help="explicit run/jNNN/attempt-NAME relative to source root")
    parser.add_argument("--destination", required=True, type=Path, help="new empty bundle directory outside source root")
    args = parser.parse_args()
    try:
        summary = export(args.source_root, args.attempt, args.destination)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
