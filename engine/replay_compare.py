"""Conservative, read-only comparisons for recorded sandbox workloads.

Output normalization is deliberately narrow. Unrecognized differences remain
visible; numeric results, test names, errors, paths and exit codes stay intact.
This module performs no replay, file writes, API requests or container actions.
"""
from __future__ import annotations

import difflib
import re
from typing import Any


_SECONDS = r"\d+(?:\.\d+)?"
_RULES = (
    (
        "unittest_summary_duration",
        re.compile(rf"^(Ran \d+ tests? in ){_SECONDS}(s)$", re.MULTILINE),
        r"\g<1><SECONDS>\g<2>",
    ),
    (
        "pytest_summary_duration",
        re.compile(
            rf"^(?P<prefix>(?:=+ )?(?:\d+ (?:passed|failed|skipped|deselected|"
            rf"xfailed|xpassed|errors?|warnings?)(?:, )?)+ in ){_SECONDS}"
            rf"s(?: \(\d+:\d{{2}}(?::\d{{2}})?\))?(?P<suffix>(?: =+)?)$",
            re.MULTILINE,
        ),
        r"\g<prefix><SECONDS>s\g<suffix>",
    ),
    (
        "python_object_address",
        re.compile(r"(<[A-Za-z_][\w.]*(?: object) at )0x[0-9a-fA-F]+(>)"),
        r"\g<1><ADDRESS>\g<2>",
    ),
)


def _normalize_output(value: str) -> tuple[str, set[str]]:
    used = set()
    for name, pattern, replacement in _RULES:
        value, count = pattern.subn(replacement, value)
        if count:
            used.add(name)
    return value, used


def _excerpt(expected: str, actual: str) -> str:
    lines = list(difflib.unified_diff(
        expected.splitlines(keepends=True), actual.splitlines(keepends=True),
        fromfile="expected", tofile="actual", n=2,
    ))
    excerpt = "".join(lines[:40])
    if len(lines) > 40:
        excerpt += "\n[additional diff lines omitted]\n"
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000] + "\n[additional diff characters omitted]\n"
    if not excerpt and expected != actual:
        # splitlines discards the distinction between several trailing-newline
        # arrangements; preserve evidence for every unequal pair.
        excerpt = f"expected trailing text: {expected[-200:]!r}\nactual trailing text: {actual[-200:]!r}"
    return excerpt


def compare_output(expected: str, actual: str) -> dict[str, Any]:
    """Return raw and narrowly normalized equality plus remaining difference."""
    if not isinstance(expected, str) or not isinstance(actual, str):
        raise TypeError("compare_output requires two strings")
    norm_expected, expected_rules = _normalize_output(expected)
    norm_actual, actual_rules = _normalize_output(actual)
    return {
        "raw_equal": expected == actual,
        "normalized_equal": norm_expected == norm_actual,
        "normalization_rules": sorted(expected_rules | actual_rules),
        "diff_excerpt": _excerpt(norm_expected, norm_actual),
    }


_EVAL_VOLATILE_KEYS = frozenset({"resources", "elapsed_s"})


def _stable_eval(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_eval(item, (*path, key))
            for key, item in sorted(value.items())
            if key not in _EVAL_VOLATILE_KEYS or "tests_status" in path
        }
    if isinstance(value, list):
        result = [_stable_eval(item, path) for item in value]
        if "tests_status" in path and all(isinstance(item, str) for item in result):
            # Status buckets represent test membership. Preserve duplicates so
            # missing or duplicate cases still produce a mismatch.
            return sorted(result)
        return result
    return value


def _differences(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    if type(expected) is not type(actual):
        return [{"path": path, "expected": expected, "actual": actual}]
    if isinstance(expected, dict):
        changes = []
        for key in sorted(expected.keys() | actual.keys()):
            child = f"{path}.{key}"
            if key not in expected:
                changes.append({"path": child, "expected_missing": True, "actual": actual[key]})
            elif key not in actual:
                changes.append({"path": child, "expected": expected[key], "actual_missing": True})
            else:
                changes.extend(_differences(expected[key], actual[key], child))
        return changes
    if isinstance(expected, list):
        changes = []
        if len(expected) != len(actual):
            changes.append({"path": path + ".length", "expected": len(expected), "actual": len(actual)})
        for i, (left, right) in enumerate(zip(expected, actual)):
            changes.extend(_differences(left, right, f"{path}[{i}]"))
        return changes
    if expected != actual:
        return [{"path": path, "expected": expected, "actual": actual}]
    return []


def compare_eval(expected: dict, actual: dict) -> dict[str, Any]:
    """Compare same-shaped grader reports, preserving every test/status field.

    Accept either per-instance reports or equally wrapped reports. Only explicit
    resource snapshots and elapsed_s are omitted. New fields stay significant.
    """
    if not isinstance(expected, dict) or not isinstance(actual, dict):
        raise TypeError("compare_eval requires two dictionaries")
    stable_expected, stable_actual = _stable_eval(expected), _stable_eval(actual)
    changes = _differences(stable_expected, stable_actual)
    return {
        "equal": not changes,
        "differences": changes,
        "expected_stable": stable_expected,
        "actual_stable": stable_actual,
        "excluded_fields": sorted(_EVAL_VOLATILE_KEYS),
    }
