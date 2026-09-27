import copy
import unittest

from engine.replay_compare import compare_eval, compare_output


class OutputComparisonTests(unittest.TestCase):
    def test_exact_output(self):
        result = compare_output("value=42\n", "value=42\n")
        self.assertTrue(result["raw_equal"])
        self.assertTrue(result["normalized_equal"])
        self.assertEqual(result["diff_excerpt"], "")

    def test_only_standard_test_duration_is_normalized(self):
        before = "Ran 12 tests in 0.002s\n=== 3 passed, 1 skipped in 1.43s ===\n"
        after = "Ran 12 tests in 5.92s\n=== 3 passed, 1 skipped in 22.10s ===\n"
        result = compare_output(before, after)
        self.assertFalse(result["raw_equal"])
        self.assertTrue(result["normalized_equal"])
        self.assertEqual(result["normalization_rules"], ["pytest_summary_duration", "unittest_summary_duration"])

    def test_pytest_long_duration_and_plain_summary(self):
        self.assertTrue(compare_output("1 passed in 0.02s", "1 passed in 65.10s (0:01:05)")["normalized_equal"])

    def test_test_counts_names_errors_and_numeric_values_are_preserved(self):
        for before, after in [
            ("1 passed in 0.1s", "2 passed in 0.2s"),
            ("test_alpha PASSED", "test_beta PASSED"),
            ("1 failed in 0.1s", "1 passed in 0.2s"),
            ("Ran 2 tests in 0.1s\nERROR: x", "Ran 2 tests in 0.2s\nERROR: y"),
            ("value=0.123", "value=0.456"),
            ("time=1.23s", "time=4.56s"),
            ("AssertionError: 0x123", "AssertionError: 0x456"),
        ]:
            with self.subTest(before=before):
                result = compare_output(before, after)
                self.assertFalse(result["normalized_equal"])
                self.assertTrue(result["diff_excerpt"])

    def test_object_addresses_only(self):
        self.assertTrue(compare_output("<package.X object at 0x123>", "<package.X object at 0xabc>")["normalized_equal"])
        self.assertFalse(compare_output("<package.X object at 0x123>", "<package.Y object at 0xabc>")["normalized_equal"])
        self.assertFalse(compare_output("hash: 0x123", "hash: 0xabc")["normalized_equal"])

    def test_paths_whitespace_and_timestamps_stay_significant(self):
        for left, right in [("/tmp/a", "/tmp/b"), ("ok\n", "ok"), ("2026-01-01", "2026-01-02")]:
            with self.subTest(left=left):
                self.assertFalse(compare_output(left, right)["normalized_equal"])


class EvaluationComparisonTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "resolved": True,
            "patch_successfully_applied": True,
            "eval_returncode": 0,
            "elapsed_s": 1.1,
            "resources": {"cgroup": {"memory.peak": 123}},
            "tests_status": {"FAIL_TO_PASS": {"success": ["test_b", "test_a"], "failure": []}},
        }

    def test_ignore_resources_duration_and_test_bucket_order(self):
        other = copy.deepcopy(self.report)
        other.update(elapsed_s=9.9, resources={"cgroup": {"memory.peak": 999}})
        other["tests_status"]["FAIL_TO_PASS"]["success"].reverse()
        self.assertTrue(compare_eval(self.report, other)["equal"])
        self.assertEqual(self.report["tests_status"]["FAIL_TO_PASS"]["success"], ["test_b", "test_a"])

    def test_same_resolved_with_different_test_result_fails(self):
        other = copy.deepcopy(self.report)
        other["tests_status"]["FAIL_TO_PASS"] = {"success": ["test_a"], "failure": ["test_b"]}
        result = compare_eval(self.report, other)
        self.assertFalse(result["equal"])
        self.assertTrue(any("tests_status" in item["path"] for item in result["differences"]))

    def test_missing_duplicate_and_returncode_remain_significant(self):
        for kind in ["missing", "duplicate", "returncode", "new_field"]:
            other = copy.deepcopy(self.report)
            if kind == "missing":
                del other["tests_status"]
            elif kind == "duplicate":
                other["tests_status"]["FAIL_TO_PASS"]["success"].append("test_a")
            elif kind == "returncode":
                other["eval_returncode"] = 124
            else:
                other["error"] = "timeout"
            with self.subTest(kind=kind):
                self.assertFalse(compare_eval(self.report, other)["equal"])

    def test_types_and_non_test_array_order_remain_significant(self):
        self.assertFalse(compare_eval({"resolved": True}, {"resolved": 1})["equal"])
        self.assertFalse(compare_eval({"commands": ["a", "b"]}, {"commands": ["b", "a"]})["equal"])

    def test_wrapped_report_and_test_named_resources(self):
        self.assertTrue(compare_eval({"task": self.report}, {"task": copy.deepcopy(self.report)})["equal"])
        left = {"tests_status": {"resources": "PASSED"}}
        right = {"tests_status": {"resources": "FAILED"}}
        self.assertFalse(compare_eval(left, right)["equal"])


if __name__ == "__main__":
    unittest.main()
