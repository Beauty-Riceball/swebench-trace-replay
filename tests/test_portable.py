"""Offline checks of portability boundaries, image identity, and cleanup scope."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engine.bundle import load_bundle
from engine.replay import ReplayValidationError, load_frozen_runtime, sha256
from runtime.full500_runtime import (ContainerCleanupError, ContainerCreationUncertain,
    LABEL, TOOL_ENV, cleanup_uncertain_creation, remove_container)
from runtime.preflight import docker_preflight, ensure_image
from runtime.topology import discover_topology, parse_cpu_list, select_slots
import test_replay as fixtures
from scripts import replay as cli

ROOT = Path(__file__).resolve().parents[1]


def response(returncode=0, value=None, stderr=''):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(value), stderr=stderr)


class PortableTests(unittest.TestCase):
    def test_topology_nonzero_cpuset_and_smt(self):
        topology = [
            {'cpu': 32, 'node': 2, 'core': (1, 0)},
            {'cpu': 33, 'node': 2, 'core': (1, 1)},
            {'cpu': 96, 'node': 2, 'core': (1, 0)},
            {'cpu': 97, 'node': 2, 'core': (1, 1)},
        ]
        self.assertEqual(select_slots(topology, 1), [{'cpus': '32,33', 'numa_node': '2', 'physical_pair': 0}])
        self.assertEqual(select_slots(topology, 1, '96,97')[0]['cpus'], '96,97')
        with self.assertRaisesRegex(ValueError, 'physical cores'):
            select_slots(topology, 2)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            select_slots(topology, 1, '0,1')
        self.assertEqual(parse_cpu_list('2,4-6'), {2, 4, 5, 6})
        with self.assertRaises(ValueError):
            parse_cpu_list('5-3')

    def test_topology_actual_sysfs_shape_and_memory_node_constraint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def write(path, value):
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value)
            write('sys/devices/system/cpu/online', '8-11')
            write('sys/devices/system/node/node1/cpulist', '8-9')
            write('sys/devices/system/node/node2/cpulist', '10-11')
            write('proc/self/status', 'Mems_allowed_list:\t2\n')
            for cpu in range(8, 12):
                write(f'sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id', '0')
                write(f'sys/devices/system/cpu/cpu{cpu}/topology/core_id', str(cpu))
            found = discover_topology(root / 'sys', root / 'proc', {8, 9, 10, 11, 12})
            self.assertEqual([item['cpu'] for item in found], [10, 11])
            self.assertEqual(select_slots(found, 1)[0]['numa_node'], '2')

    def test_cleanup_checks_labels_before_removing_and_confirms_id_absent(self):
        calls = []
        owned = {'Name': '/str-session-j000-agent', 'Id': 'a' * 64,
                 'Config': {'Labels': {LABEL: 'true', LABEL + '.session': 'session'}}}
        replies = [response(value=[owned]), response(), response(1, stderr='Error: No such object')]
        def run(argv, **kwargs):
            calls.append(argv)
            return replies.pop(0)
        remove_container(SimpleNamespace(run=run), 'str-session-j000-agent', 'session')
        self.assertEqual(calls[1], ['docker', 'rm', '-f', 'a' * 64])
        self.assertEqual(calls[2], ['docker', 'inspect', 'a' * 64])
        calls.clear()
        owned['Config']['Labels'][LABEL + '.session'] = 'someone-else'
        replies[:] = [response(value=[owned])]
        with self.assertRaisesRegex(ContainerCleanupError, 'not owned'):
            remove_container(SimpleNamespace(run=run), 'str-session-j000-agent', 'session')
        self.assertEqual(len(calls), 1)

    def test_cleanup_unconfirmed_removal_is_failure(self):
        owned = {'Name': '/container', 'Id': 'id', 'Config': {'Labels': {LABEL: 'true', LABEL + '.session': 's'}}}
        replies = [response(value=[owned]), response(), response(value=[owned])]
        with self.assertRaisesRegex(ContainerCleanupError, 'confirmed'):
            remove_container(SimpleNamespace(run=lambda *a, **kw: replies.pop(0)), 'container', 's')

    def test_timed_out_creation_late_arrival_is_removed_but_remains_uncertain(self):
        owned = {'Name': '/late', 'Id': 'late-id', 'Config': {'Labels': {LABEL: 'true', LABEL + '.session': 's'}}}
        replies = [response(1, stderr='No such object'), response(value=[owned]),
                   response(), response(1, stderr='No such object'), response(1, stderr='No such object')]
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return replies.pop(0)
        with self.assertRaisesRegex(ContainerCreationUncertain, 'final cleanup remain uncertain'):
            cleanup_uncertain_creation(SimpleNamespace(run=run), 'late', 's', delay=0)
        self.assertIn(['docker', 'rm', '-f', 'late-id'], calls)
        self.assertEqual(len(calls), 5)

    def test_timed_out_creation_missing_in_all_probes_remains_uncertain(self):
        calls = []
        stop = threading.Event()
        def run(argv, **kwargs):
            self.assertTrue(stop.is_set())
            calls.append(argv)
            return response(1, stderr='No such object')
        with patch('runtime.full500_runtime.STOP_EVENT', stop), \
                self.assertRaisesRegex(ContainerCreationUncertain, 'backend completion'):
            cleanup_uncertain_creation(SimpleNamespace(run=run), 'late', 's', delay=0)
        self.assertEqual(len(calls), 3)

    def test_preflight_requires_containerd_image_store_for_recorded_ids(self):
        info = {'OSType': 'linux', 'CgroupVersion': '2', 'Architecture': 'x86_64', 'DriverStatus': []}
        base = SimpleNamespace(run=lambda *a, **kw: response(value=info))
        with patch('runtime.preflight.platform.system', return_value='Linux'), \
                patch('runtime.preflight.platform.machine', return_value='x86_64'), \
                patch('runtime.preflight.Path.is_file', return_value=True), \
                patch.dict('os.environ', {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': ''}):
            with self.assertRaisesRegex(RuntimeError, 'containerd image store'):
                docker_preflight(base)
            info['DriverStatus'] = [['driver-type', 'io.containerd.snapshotter.v1']]
            self.assertEqual(docker_preflight(base)['CgroupVersion'], '2')

    def test_docker_context_precedence_cannot_hide_remote_endpoint(self):
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return response(value=[{'Endpoints': {'docker': {'Host': 'ssh://remote'}}}])
        with patch('runtime.preflight.platform.system', return_value='Linux'), \
                patch('runtime.preflight.platform.machine', return_value='x86_64'), \
                patch('runtime.preflight.Path.is_file', return_value=True), \
                patch.dict('os.environ', {'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': 'selected'}):
            with self.assertRaisesRegex(RuntimeError, 'local Unix-socket'):
                docker_preflight(SimpleNamespace(run=run))
        self.assertEqual(calls, [['docker', 'context', 'inspect', 'selected']])

    def test_uncertain_creation_stops_queue_and_marks_summary(self):
        case = self.fixture()
        validated, _ = load_bundle(case.root)
        class UncertainEnvironment:
            def __init__(self, *args, **kwargs):
                raise ContainerCreationUncertain('creation remains uncertain')
        class Sink:
            def __init__(self, *args, **kwargs):
                pass
            def start(self):
                return self
            def stop(self):
                pass
            def close(self):
                pass
        base = SimpleNamespace(Log=Sink, Monitor=Sink)
        full = SimpleNamespace(make_environment_class=lambda base: UncertainEnvironment, TOOL_ENV=TOOL_ENV)
        second = {**validated[0], 'job': {**validated[0]['job'], 'job_id': 'j001'}}
        output = Path(case.tmp.name) / 'execution'
        output.mkdir()
        with patch('scripts.replay.signal.signal'), patch('builtins.print'):
            code = cli.execute(SimpleNamespace(concurrency=1, wait_scale=0),
                               [validated[0], second], output, (base, full))
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual(code, 1)
        self.assertEqual(summary['state'], 'interrupted')
        self.assertEqual(summary['finished'], 1)
        self.assertEqual(summary['cleanup_state'], 'uncertain')
        self.assertFalse((output / 'j001').exists())

    def test_image_uses_recorded_id_and_refuses_tag_drift(self):
        expected = 'sha256:' + 'a' * 64
        image = {'image_id': expected, 'source_image': 'example/task:latest'}
        calls = []
        replies = [response(1), response(), response(value=[{'Id': 'sha256:' + 'b' * 64, 'Architecture': 'amd64'}])]
        def run(argv, **kwargs):
            calls.append(argv)
            return replies.pop(0)
        with self.assertRaisesRegex(RuntimeError, 'ID drift'):
            ensure_image(SimpleNamespace(run=run), image, prepare=True)
        self.assertEqual(calls[0], ['docker', 'image', 'inspect', expected])
        self.assertEqual(calls[1], ['docker', 'pull', 'example/task:latest'])

    def test_replay_runtime_keeps_paid_operations_disabled(self):
        base, full = load_frozen_runtime()
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            base.read_key()
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            base.DeepSeekModel()
        self.assertEqual(full.TOOL_ENV['OMP_NUM_THREADS'], '2')
        self.assertNotIn('deepseek_model', sys.modules)

    def fixture(self):
        case = fixtures.ReplayTests()
        case.setUp()
        self.addCleanup(case.tmp.cleanup)
        case.env = TOOL_ENV
        case.fixture()
        case.record['attempt_path'] = str(case.attempt.relative_to(case.root))
        fixtures.put(case.attempt / 'result.json', case.record)
        task = {'job_id': 'j000', 'instance_id': 'repo-1', 'attempt_path': case.record['attempt_path']}
        files = [{'path': str(path.relative_to(case.root)), 'sha256': sha256(path), 'size_bytes': path.stat().st_size}
                 for path in case.attempt.rglob('*') if path.is_file()]
        fixtures.put(case.root / 'bundle-manifest.json', {'schema_version': 1, 'kind': 'sanitized-trace-bundle', 'tasks': [task], 'files': files})
        return case

    def test_manifest_drift_rejected_before_docker(self):
        case = self.fixture()
        validated, manifest = load_bundle(case.root)
        self.assertEqual(len(validated), 1)
        (case.attempt / 'eval.sh').write_text('modified')
        with self.assertRaisesRegex(ReplayValidationError, 'bundle SHA256'):
            load_bundle(case.root)

    def test_validate_cli_without_site_packages_or_docker(self):
        case = self.fixture()
        output = Path(case.tmp.name) / 'validation'
        # -S excludes site-packages; PATH makes Docker unavailable.
        completed = subprocess.run([sys.executable, '-S', str(ROOT / 'scripts/replay.py'),
            '--source', str(case.root), '--output', str(output), '--validate-only'],
            capture_output=True, text=True, env={'PATH': '/nonexistent'})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads((output / 'validation.json').read_text())
        self.assertTrue(report['bundle_manifest_verified'])
        self.assertEqual(report['model_calls'], 0)
        self.assertEqual(report['jobs'], 1)

    def test_manifest_unlisted_attempt_file_rejected(self):
        case = self.fixture()
        (case.attempt / 'extra-command.txt').write_text('unmanifested')
        with self.assertRaisesRegex(ReplayValidationError, 'missing from manifest'):
            load_bundle(case.root)


if __name__ == '__main__':
    unittest.main()
