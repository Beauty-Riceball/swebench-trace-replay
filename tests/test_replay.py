import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from engine.replay import (ReplayValidationError, forbidden_paid_operation,
                           parse_submission, replay_one, validate_attempt)


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class NeverStop:
    def is_set(self):
        return False

    def wait(self, timeout):
        return False


class Submitted(Exception):
    pass


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'source'
        self.attempt = self.root / 'run/j000/attempt-a'
        self.attempt.mkdir(parents=True)
        self.image_id = 'sha256:' + 'a' * 64
        self.patch = 'diff --git a/x b/x\n+new\n'
        self.env = {'A': 'B'}
        self.events = []

    def event(self, kind, **kwargs):
        data = {'sequence': len(self.events) + 1, 'type': kind, **kwargs}
        self.events.append(data)
        return data

    def tool(self, phase, index, command, output, returncode=0, submit=False, submitted=False):
        self.event('tool_request', phase=phase, tool_index=index, action={'command': command},
                   cwd='/testbed', environment=self.env, timeout_s=300, submit_enabled=submit)
        name = f'{phase}-tool-{index:03d}.log'
        (self.attempt / name).write_text(output)
        self.event('tool_result', phase=phase, tool_index=index,
                   result=None if submitted else {'output': output, 'returncode': returncode, 'exception_info': ''},
                   exception_type='Submitted' if submitted else None, output_file=name,
                   output_sha256=hashlib.sha256(output.encode()).hexdigest())

    def fixture(self, limited=False):
        self.tool('agent', 1, 'probe', 'head\n')
        self.event('api_request')
        self.event('api_response', elapsed_seconds=1.2)
        if limited:
            self.tool('agent', 2, 'timeout command', '', returncode=124, submit=True)
            self.tool('agent', 3, 'git diff --binary', self.patch)
        else:
            self.tool('agent', 2, 'submit', 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n' + self.patch, submit=True, submitted=True)
        self.tool('eval', 1, 'git apply /tmp/prediction.patch', 'applied\n')
        self.tool('eval', 2, '/bin/bash /eval.sh', 'passed\n')
        (self.attempt / 'trajectory.json').write_text('{}')
        (self.attempt / 'prediction.patch').write_text(self.patch)
        (self.attempt / 'eval.sh').write_text('echo passed')
        self.eval = {'resolved': True, 'tests_status': {'FAIL_TO_PASS': {'success': ['test_x'], 'failure': []}}, 'eval_returncode': 0}
        self.record = {'job_id': 'j000', 'instance_id': 'repo-1', 'state': 'completed', 'capture_complete': True,
                       'attempt_path': str(self.attempt), 'trajectory_sha256': hashlib.sha256(b'{}').hexdigest(),
                       'trace_events_saved': len(self.events), 'api_responses_saved': 1,
                       'exit_status': 'LimitsExceeded' if limited else 'Submitted', 'evaluation': self.eval}
        put(self.attempt / 'result.json', self.record)
        put(self.attempt / 'task.json', {'instance_id': 'repo-1'})
        put(self.attempt / 'agent-environment.json', {'instance_id': 'repo-1',
            'limits': {'cpus': 2, 'memory_bytes': 4 * 1024**3, 'swap_bytes': 0},
            'image': {'image': self.image_id, 'image_id': self.image_id}})
        self.save_events()
        self.job = {'job_id': 'j000', 'instance_id': 'repo-1', 'attempt_path': str(self.attempt)}
        return validate_attempt(self.root, self.job)

    def save_events(self):
        for event in self.events:
            put(self.attempt / 'trace' / f"{event['sequence']:06d}-{event['type']}.json", event)

    def runtime(self, altered_submit=False, failure_command=None):
        outer = self
        calls, copies, cleaned = [], [], []

        class FakeEnv:
            def __init__(self, task, slot, job, phase, out, *args):
                self.phase, self.out = phase, out
                self.container_id = 'container-' + phase
                self.index = 0

            def execute(self, action, cwd, timeout, submit):
                self.index += 1
                command = action['command']
                calls.append((self.phase, command, cwd, timeout, submit))
                if command == failure_command:
                    raise RuntimeError('infrastructure failure')
                expected = next(e for e in outer.events if e['type'] == 'tool_result' and e['phase'] == self.phase and e['tool_index'] == self.index)
                output = (outer.attempt / expected['output_file']).read_text()
                if command == 'submit' and altered_submit:
                    output = output.replace('+new', '+different')
                (self.out / f'{self.phase}-tool-{self.index:03d}.log').write_text(output)
                if expected['exception_type'] == 'Submitted':
                    raise Submitted()
                return expected['result']

            def copy(self, source, dest):
                copies.append((self.phase, Path(source).read_text(), dest))

            def cleanup(self):
                cleaned.append(self.phase)
                return {'cleaned': True}

        base = SimpleNamespace(Submitted=Submitted,
            run=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps([{'Image': self.image_id}]), stderr=''),
            make_test_spec=lambda task: task,
            get_eval_report=lambda *args, **kwargs: {'repo-1': json.loads(json.dumps(self.eval))})
        full = SimpleNamespace(make_environment_class=lambda base: FakeEnv, TOOL_ENV=self.env)
        return (base, full), calls, copies, cleaned

    def run_fixture(self, validated, **kwargs):
        runtime, calls, copies, cleaned = self.runtime(**kwargs)
        out = Path(self.tmp.name) / 'replayed'
        out.mkdir()
        result = replay_one(validated, 0, out, runtime, None, None, 1.0, NeverStop(), 'sample')
        return result, calls, copies, cleaned, out

    def test_submitted_real_execution_patch_and_fresh_eval(self):
        result, calls, copies, cleaned, out = self.run_fixture(self.fixture())
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['normalized_consistent'])
        self.assertEqual(cleaned, ['agent', 'eval'])
        self.assertEqual([call[0] for call in calls], ['agent', 'agent', 'eval', 'eval'])
        self.assertIn(('eval', self.patch, '/tmp/prediction.patch'), copies)
        self.assertIn(('eval', 'echo passed', '/eval.sh'), copies)
        self.assertEqual((out / 'j000/prediction.patch').read_text(), self.patch)
        self.assertEqual(result['api_waits'][0]['requested_s'], 1.2)

    def test_different_actual_submission_is_preserved_and_eval_continues(self):
        result, calls, copies, cleaned, out = self.run_fixture(self.fixture(), altered_submit=True)
        self.assertFalse(result['patch_equal'])
        self.assertFalse(result['functional_equal'])
        self.assertEqual(result['state'], 'completed')
        self.assertIn('+different', (out / 'j000/prediction.patch').read_text())
        self.assertIn('+different', copies[0][1])
        self.assertEqual(calls[-1][1], '/bin/bash /eval.sh')

    def test_limits_timeout_and_final_diff_are_reexecuted(self):
        result, calls, _, cleaned, _ = self.run_fixture(self.fixture(limited=True))
        self.assertTrue(result['normalized_consistent'])
        self.assertEqual(result['patch_source'], 'replayed_working_tree_diff')
        self.assertIn(('agent', 'timeout command', '/testbed', 300, True), calls)
        self.assertEqual(result['tool_comparisons'][1]['actual_returncode'], 124)

    def test_infrastructure_failure_stops_job_and_cleans_container(self):
        result, calls, _, cleaned, _ = self.run_fixture(self.fixture(), failure_command='submit')
        self.assertEqual(result['state'], 'error')
        self.assertEqual(cleaned, ['agent'])
        self.assertEqual(calls[-1][1], 'submit')

    def test_corrupted_output_is_rejected_before_execution(self):
        self.fixture()
        (self.attempt / 'agent-tool-001.log').write_text('corrupted')
        with self.assertRaisesRegex(ReplayValidationError, 'SHA256'):
            validate_attempt(self.root, self.job)

    def test_sequence_gap_is_rejected(self):
        self.fixture()
        first = next((self.attempt / 'trace').glob('000001-*'))
        first.unlink()
        with self.assertRaisesRegex(ReplayValidationError, 'sequence'):
            validate_attempt(self.root, self.job)

    def test_trajectory_corruption_and_wrong_image_are_rejected(self):
        self.fixture()
        self.job['image'] = 'sha256:' + 'b' * 64
        with self.assertRaisesRegex(ReplayValidationError, 'image'):
            validate_attempt(self.root, self.job)
        del self.job['image']
        (self.attempt / 'trajectory.json').write_text('{"changed":true}')
        with self.assertRaisesRegex(ReplayValidationError, 'trajectory'):
            validate_attempt(self.root, self.job)

    def test_path_escape_and_paid_operations_rejected(self):
        self.fixture()
        self.job['attempt_path'] = str(Path(self.tmp.name))
        with self.assertRaisesRegex(ReplayValidationError, 'outside'):
            validate_attempt(self.root, self.job)
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            forbidden_paid_operation()

    def test_eval_environment_identity_mismatch_rejected(self):
        self.fixture()
        environment = json.loads((self.attempt / 'agent-environment.json').read_text())
        environment['image']['image_id'] = 'sha256:' + 'b' * 64
        put(self.attempt / 'eval-environment.json', environment)
        with self.assertRaisesRegex(ReplayValidationError, 'evaluation image'):
            validate_attempt(self.root, self.job)

    def test_original_apply_failure_needs_no_script_or_grader(self):
        self.fixture()
        self.events = [event for event in self.events if not (event.get('phase') == 'eval' and event.get('tool_index') == 2)]
        apply_result = self.events[-1]
        apply_result['result']['returncode'] = 1
        self.record['trace_events_saved'] = len(self.events)
        self.record['evaluation'] = {'resolved': False, 'error': 'patch_apply_failed'}
        self.eval = self.record['evaluation']
        put(self.attempt / 'result.json', self.record)
        for path in (self.attempt / 'trace').glob('*.json'):
            path.unlink()
        self.save_events()
        (self.attempt / 'eval.sh').unlink()
        validated = validate_attempt(self.root, self.job)
        result, calls, copies, cleaned, _ = self.run_fixture(validated)
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['functional_equal'])
        self.assertEqual(result['evaluation'], self.eval)
        self.assertFalse(any(dest == '/eval.sh' for _, _, dest in copies))

    def test_original_empty_patch_replays_final_diff_and_skips_eval(self):
        self.patch = ''
        self.fixture(limited=True)
        self.events = [event for event in self.events if event.get('phase') != 'eval']
        self.record['trace_events_saved'] = len(self.events)
        self.record['evaluation'] = {'resolved': False, 'error': 'empty_patch'}
        put(self.attempt / 'result.json', self.record)
        for path in (self.attempt / 'trace').glob('*.json'):
            path.unlink()
        self.save_events()
        (self.attempt / 'eval.sh').unlink()
        validated = validate_attempt(self.root, self.job)
        result, calls, copies, cleaned, out = self.run_fixture(validated)
        self.assertTrue(result['functional_equal'])
        self.assertEqual(cleaned, ['agent'])
        self.assertEqual(copies, [])
        self.assertEqual((out / 'j000/prediction.patch').read_text(), '')

    def test_submission_sentinel_is_required(self):
        self.assertEqual(parse_submission('  COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\npatch\n'), 'patch\n')
        with self.assertRaises(ReplayValidationError):
            parse_submission('some output')


if __name__ == '__main__':
    unittest.main()
