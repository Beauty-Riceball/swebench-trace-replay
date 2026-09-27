#!/usr/bin/env python3
"""Inspect an exported trace without Docker, third-party packages, or API calls."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys


SECRET_PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "api-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}"),
    "google-api-key": re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
    "bearer-token": re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    "credential-url": re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"),
    "credential-assignment": re.compile(
        r'''(?ix)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|aws[_-]?secret[_-]?access[_-]?key)
        ["']?\s*[:=]\s*["']([A-Za-z0-9_+./=-]{20,})["']'''
    ),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_child(root: Path, name: str) -> Path:
    """Reject traversal and symlinks before opening untrusted manifest paths."""
    rel = PurePosixPath(name)
    if not name or rel.is_absolute() or any(p in ("", ".", "..") for p in rel.parts):
        raise ValueError("unsafe relative path")
    if "\\" in name or str(rel) != name:
        raise ValueError("non-canonical relative path")
    candidate = root.joinpath(*rel.parts)
    current = root
    for component in rel.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("symlink in bundle path")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escapes bundle")
    return candidate


def credential_findings(files: dict[str, bytes]) -> list[dict[str, str]]:
    """Return only categories and relative filenames, never matching values."""
    findings = []
    for name, data in sorted(files.items()):
        text = data.decode("utf-8", errors="replace")
        for category, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append({"category": category, "path": name})
    return findings


def inspect_bundle(root: Path, scan: bool = False, command_limit: int = 3) -> dict:
    root = root.resolve()
    manifest_path = safe_child(root, "bundle-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "sanitized-trace-bundle":
        raise ValueError("unsupported bundle manifest")
    files: dict[str, bytes] = {}
    for entry in manifest["files"]:
        name = entry["path"]
        if name in files:
            raise ValueError("duplicate manifest path")
        data = safe_child(root, name).read_bytes()
        if digest(data) != entry["sha256"] or len(data) != entry["size_bytes"]:
            raise ValueError(f"manifest checksum mismatch: {name}")
        files[name] = data
    inventory = list(root.rglob("*"))
    if any(p.is_symlink() for p in inventory):
        raise ValueError("bundle contains a symlink")
    actual = {p.relative_to(root).as_posix() for p in inventory if p.is_file()}
    if actual != set(files) | {"bundle-manifest.json"}:
        raise ValueError("bundle contains missing or unlisted files")

    if not manifest["tasks"]:
        raise ValueError("manifest has no tasks")
    summaries = []
    for task in manifest["tasks"]:
        attempt_name = task["attempt_path"]
        attempt = safe_child(root, attempt_name)
        result = json.loads(files[f"{attempt_name}/result.json"])
        trajectory = files[f"{attempt_name}/trajectory.json"]
        if result["trajectory_sha256"] != digest(trajectory):
            raise ValueError("trajectory checksum mismatch")
        if (result["attempt_path"] != attempt_name or result["job_id"] != task["job_id"]
                or result["instance_id"] != task["instance_id"]):
            raise ValueError("task identity mismatch")
        events = [json.loads(p.read_text()) for p in sorted((attempt / "trace").glob("*.json"))]
        if [e["sequence"] for e in events] != list(range(1, len(events) + 1)):
            raise ValueError("non-contiguous trace sequence")
        kinds = Counter(e["type"] for e in events)
        requests, responses = {}, {}
        api_requests, api_responses = {}, {}
        for event in events:
            kind = event["type"]
            if kind in ("tool_request", "tool_result"):
                key = (event["phase"], event["tool_index"])
                target = requests if kind == "tool_request" else responses
                if key in target:
                    raise ValueError("duplicate tool event")
                target[key] = event
                if kind == "tool_result":
                    data = safe_child(attempt, event["output_file"]).read_bytes()
                    if digest(data) != event["output_sha256"]:
                        raise ValueError("tool log checksum mismatch")
                    if event["result"] is None and not event.get("exception_type"):
                        raise ValueError("tool result lacks output and recorded exception")
                    if event["result"] is not None and data != event["result"]["output"].encode("utf-8"):
                        raise ValueError("tool log differs from recorded output")
            elif kind in ("api_request", "api_response"):
                target = api_requests if kind == "api_request" else api_responses
                if event["request_id"] in target:
                    raise ValueError("duplicate API event")
                target[event["request_id"]] = event
        if requests.keys() != responses.keys() or api_requests.keys() != api_responses.keys():
            raise ValueError("unpaired trace events")
        if (not result.get("capture_complete") or kinds["api_response"] != result["model_calls"]
                or len(events) != result["trace_events_saved"]
                or kinds["api_response"] != result["api_responses_saved"]
                or sum(e["phase"] == "agent" for e in responses.values()) != result["tool_results_saved"]):
            raise ValueError("capture is incomplete")
        billing_ids = [e["request_id"] for e in events if e["type"] == "api_billing"]
        if Counter(billing_ids) != Counter(api_requests.keys()):
            raise ValueError("unpaired API billing events")
        for key, req in requests.items():
            if req["sequence"] >= responses[key]["sequence"]:
                raise ValueError("tool result precedes request")
        examples = [
            {"sequence": e["sequence"], "phase": e["phase"], "tool_index": e["tool_index"],
             "command": e["action"]["command"]}
            for e in requests.values()
            if e["phase"] == "agent" and e.get("submit_enabled")
        ][:command_limit]
        summaries.append({
            "job_id": task["job_id"], "instance_id": task["instance_id"],
            "attempt_path": attempt_name, "trace_events": len(events),
            "llm_calls": kinds["api_response"], "tool_calls": kinds["tool_request"],
            "agent_tool_calls": sum(e["phase"] == "agent" for e in requests.values()),
            "eval_tool_calls": sum(e["phase"] == "eval" for e in requests.values()),
            "llm_response_wait_seconds": sum(e["elapsed_seconds"] for e in api_responses.values()),
            "capture_resolved": result["evaluation"]["resolved"], "example_commands": examples,
        })
    findings = credential_findings(files) if scan else None
    return {"manifest_sha256": digest(manifest_path.read_bytes()), "verified_files": len(files),
            "payload_bytes": sum(map(len, files.values())), "tasks": summaries,
            "credential_scan": {"performed": scan, "findings": findings,
                                "scope": "heuristic patterns; not a guarantee of absence"}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="directory containing bundle-manifest.json")
    parser.add_argument("--scan-credentials", action="store_true", help="report categories and paths only")
    parser.add_argument("--commands", type=int, default=3, help="number of recorded commands to display")
    parser.add_argument("--json", action="store_true", help="machine-readable inspection")
    args = parser.parse_args()
    if args.commands < 0:
        parser.error("--commands must be nonnegative")
    try:
        summary = inspect_bundle(args.bundle, args.scan_credentials, args.commands)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Inspection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    # Suppress command display if a credential-like value was found anywhere.
    findings = summary["credential_scan"]["findings"]
    if findings:
        for task in summary["tasks"]:
            task["example_commands"] = []
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Verified {summary['verified_files']} payload files / {summary['payload_bytes']:,} bytes")
        print(f"Manifest SHA-256: {summary['manifest_sha256']}")
        for task in summary["tasks"]:
            print(f"{task['job_id']} / {task['instance_id']}: {task['llm_calls']} LLM calls, "
                  f"{task['tool_calls']} tool calls ({task['agent_tool_calls']} agent + "
                  f"{task['eval_tool_calls']} eval), {task['trace_events']} trace events")
            print(f"Recorded LLM response wait: {task['llm_response_wait_seconds']:.3f} s")
            print(f"Captured evaluation resolved: {task['capture_resolved']}")
            for example in task["example_commands"]:
                print(f"  event {example['sequence']:06d} ({example['phase']} tool {example['tool_index']}): "
                      f"{example['command']}")
        if args.scan_credentials:
            print(f"Credential scan: {len(findings)} finding(s); pattern-based, not a guarantee")
            for finding in findings:
                print(f"  {finding['category']}: {finding['path']}")
    return 2 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
