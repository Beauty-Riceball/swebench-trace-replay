# Trace bundle format

A bundle is a derived, sanitized copy of one completed capture attempt. The
original capture is not edited or included as a second raw tree. Some payload
files, including every tool log, remain byte-for-byte identical to their source.
The manifest explicitly distinguishes the hash of each published file from the
hash of its original source file.

## Inspect the real sample

Run from the repository root; Python 3.10 or newer is sufficient. Inspection
does not execute recorded commands, contact an LLM, or start Docker.

```bash
python3 scripts/inspect_trace.py examples/django-14672 --scan-credentials
python3 scripts/inspect_trace.py examples/django-14672 --json --commands 0
```

The included canonical job `j000` is SWE-bench instance
`django__django-14672`, selected from capture group `full500-max-20260922`.
It contains:

| Item | Count |
| --- | ---: |
| LLM requests and responses | 20 pairs |
| Billing events | 20 |
| Agent tool requests and results | 35 pairs |
| Evaluation tool requests and results | 2 pairs |
| All trace events | 134 |
| Paired tool output logs | 37 |
| Payload files | 180 |
| Payload size | 9,463,176 bytes |

The 20 API responses record **46.458 seconds** in summed `elapsed_seconds`.
This is recorded LLM response waiting time, not total attempt duration or a
prediction of replay speed. The original result records a successful evaluation;
this is capture evidence, not a new replay result.

For example, trace event `000006` runs the original command
`pwd && ls -la | head -50` from `/testbed`. Event `000007` contains its result and
points to `agent-tool-002.log`. The preceding API response is in event `000004`;
its `sample-tool-0001` identifier also appears in the tool request and the
trajectory. Event `000079` applies the source edit that wraps
`self.through_fields` with `make_hashable()`. These commands are data during
inspection; execution is the separate replay workflow.

## Layout and manifest

```text
examples/django-14672/
  bundle-manifest.json
  run/j000/attempt-sample/
    task.json
    result.json
    trajectory.json
    agent-environment.json
    eval-environment.json
    prediction.patch
    eval.sh
    eval_report.json
    test_output.txt
    agent-tool-001.log ... agent-tool-035.log
    eval-tool-001.log  ... eval-tool-002.log
    trace/000001-tool_request.json ... 000134-tool_result.json
```

`bundle-manifest.json` has `schema_version: 1` and
`kind: "sanitized-trace-bundle"`. Its `tasks` list is the entry point for task
discovery; do not infer tasks from directory order. Each task records `job_id`,
`instance_id`, and a bundle-relative `attempt_path`.

Each record in `files` contains:

| Field | Meaning |
| --- | --- |
| `path` | Published file path relative to the bundle root |
| `sha256`, `size_bytes` | Hash and size of the published bytes |
| `source_path` | Original path relative to the source capture-group root |
| `source_sha256`, `source_size_bytes` | Hash and size before transformation |
| `transformations` | Applied metadata transformations; empty for unchanged bytes |

The manifest lists all payload files and excludes itself to avoid a recursive
hash. The inspector reports the manifest's SHA-256 separately. It verifies the
listed hashes and sizes, rejects unlisted files and unsafe paths, checks task
identity, pairs trace events, validates each output log, and checks
`result.json.trajectory_sha256` against the published trajectory. Source hashes
support comparison with a separately held original capture; they do not provide
an authenticity signature.

## Trace events

Every trace JSON has `sequence`, `type`, `recorded_unix_ns`, and
`recorded_monotonic_ns`. Sequence numbers are contiguous within the attempt.
The recorder timestamps order events; the monotonic clock supports durations
without relying on wall-clock synchronization.

| Event type | Relevant fields |
| --- | --- |
| `api_request` | `request_id`, `unix_ns`, `payload`, `timeout_s` |
| `api_response` | `request_id`, `provider_request_id`, `unix_ns`, `elapsed_seconds`, `response` |
| `api_billing` | `request_id`, `cost_rmb`, `cost_estimated`, `status` |
| `tool_request` | `phase`, `tool_index`, `action`, `cwd`, `environment`, `timeout_s`, `submit_enabled` |
| `tool_result` | `phase`, `tool_index`, `result`, `exception_type`, `output_file`, `output_sha256` |

API events join on `request_id`. Tool events join on `(phase, tool_index)`;
agent and evaluation tool indices start separately at 1. The first agent tool
is an environment check, so the sample has 34 model-issued tool calls plus one
agent setup tool and two evaluation tools. Tool calls also carry an opaque
`tool_call_id` where applicable, connecting them to API messages.

For a normal tool result, `result.output` is exactly the UTF-8 text in
`output_file`, and `result.returncode` records the exit code. The output log is
also hashed separately. A result may instead contain `result: null` with a
recorded `exception_type`. Sample event `000130` records `Submitted`; its output
is still retained and verified in `agent-tool-035.log`. Do not drop this final
tool event merely because `result` is null.

`trajectory.json` keeps the mini-swe-agent conversation, configuration, model
metadata, costs, and message-level tool associations. `task.json` retains the
benchmark problem, commits, tests, and reference patch. `prediction.patch` is
the agent's submitted patch, distinct from the benchmark reference patch.
`eval.sh`, `eval_report.json`, and `test_output.txt` retain evaluation evidence.
The environment files retain the immutable source image digest, base commit,
working directory, resource limits, environment variables, and network mode.

## Sanitization and preservation

The exporter changes only explicit metadata fields:

- Request, response, and tool-call identifiers receive deterministic aliases.
  All correlated references receive the same alias; the raw mapping is omitted.
- Provider system fingerprints and host process identifiers become `null`.
  Recognized host identity metadata fields also become `null` if present.
- The attempt path becomes `run/<job_id>/attempt-sample`; the capture session
  becomes `sample`, and the trajectory output path uses the published location.
- The private, unexported budget ledger path becomes `null`.
- The trajectory hash in `result.json` is recomputed after transformation.

Commands, function arguments, tool output bytes, prompt and model text, patches,
benchmark identity, timestamps, durations, image digests, and evaluation
results remain unchanged. The `/home/tom/...` traceback in this benchmark's
public problem statement is retained as source material; it is distinct from
the capture operator's private host paths, which are removed from metadata.
The sample contains 84 unchanged payload files and 96 JSON files with documented
metadata transformations. No log hashes change because no log bytes change.

The credential scanner checks common token, private-key, authorization, and
credential-assignment patterns. Reports contain only categories and relative
paths, never matching values. A match blocks export; secrets are not silently
redacted from executable commands or output. An unexpected private capture path
remaining after the permitted metadata transformations also blocks export.
Pattern scanning is an additional check, not proof that arbitrary data contains
no sensitive information.

## Export another complete attempt

Use an explicit source capture group and relative canonical attempt. The target
directory must not exist and must be outside the source capture root.

```bash
python3 scripts/export_trace.py \
  --source-root /path/to/full500-max-20260922 \
  --attempt run/j000/attempt-c4ec5400c5c1 \
  --destination /path/to/new-export
python3 scripts/inspect_trace.py /path/to/new-export --scan-credentials
```

The script reads a narrow allowlist of metadata, patch, evaluation, and trace
files plus logs referenced by tool-result events. It does not recursively copy
the source root, credential directories, budget ledgers, unrelated attempts,
or arbitrary attachments. It rejects traversal, symlinks in selected paths,
unsupported trace entries, incomplete captures, mismatched source hashes, and
unpaired request/result events. The output is written to a temporary directory,
verified, and then moved into its final location. Running the same exporter on
the same source bytes produces the same bundle bytes.

Do not treat a sanitized bundle as the original experiment directory. Keep the
original capture separately; use `source_path` and `source_sha256` when auditing
its relationship to the published derivative.
