"""Portable extraction of frozen full500_runner.make_environment_class.

Derived code, with machine-specific CPU mapping replaced by configured topology,
immutable image-ID execution, and ownership-checked, confirmed container removal.
Capture, dataset scheduling, model, and billing paths are intentionally absent.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import time

LABEL = 'swebench-trace-replay'
CONCURRENCY = 1
SLOT_MAPPINGS = []
STOP_EVENT = None
TOOL_ENV = {'BASH_ENV': '/root/.bashrc', 'PAGER': 'cat', 'PIP_PROGRESS_BAR': 'off',
            'TQDM_DISABLE': '1', 'OMP_NUM_THREADS': '2', 'OPENBLAS_NUM_THREADS': '2',
            'MKL_NUM_THREADS': '2'}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cpu_mapping(slot, concurrency):
    if concurrency != CONCURRENCY or not 0 <= slot < len(SLOT_MAPPINGS):
        raise ValueError('replay CPU topology was not configured for this slot')
    return dict(SLOT_MAPPINGS[slot])


class ContainerCleanupError(RuntimeError):
    """Do not reuse a slot until its owned container has been removed."""


class ContainerCreationUncertain(ContainerCleanupError):
    """A timed-out client cannot prove the daemon finished creating a container."""


def remove_container(base, name, session_id):
    inspected = base.run(['docker', 'inspect', name], timeout=30)
    if inspected.returncode:
        if 'No such' in inspected.stderr:
            return False
        raise ContainerCleanupError('cannot inspect container before cleanup: ' + name)
    info = json.loads(inspected.stdout)[0]
    labels = info.get('Config', {}).get('Labels') or {}
    if (info.get('Name', '').lstrip('/') != name
            or labels.get(LABEL) != 'true'
            or labels.get(LABEL + '.session') != session_id):
        raise ContainerCleanupError('refusing to remove a container not owned by this replay: ' + name)
    identifier = info['Id']
    removed = base.run(['docker', 'rm', '-f', identifier], timeout=30)
    if removed.returncode:
        raise ContainerCleanupError('container cleanup failed: ' + name)
    verified = base.run(['docker', 'inspect', identifier], timeout=30)
    if not verified.returncode or 'No such' not in verified.stderr:
        raise ContainerCleanupError('container removal could not be confirmed: ' + name)
    return True


def cleanup_uncertain_creation(base, name, session_id, attempts=3, delay=1):
    """Bounded probes can clean late arrivals but cannot prove creation ended."""
    if STOP_EVENT is not None:
        STOP_EVENT.set()
    observations = []
    for index in range(attempts):
        if index:
            time.sleep(delay)
        try:
            removed = remove_container(base, name, session_id)
            observations.append('owned container removed' if removed else 'not yet found')
        except Exception as error:
            observations.append(f'{type(error).__name__}: {error}')
    raise ContainerCreationUncertain(
        f'container creation timed out; backend completion and final cleanup remain uncertain: '
        f'{name}; session={session_id}; probes={observations}')


def make_environment_class(base):
    class FullEnvironment(base.Environment):
        def __init__(self, row, slot, job_id, phase, out, monitor, log, args, image, session_id):
            self.row, self.slot, self.job_id, self.phase = row, slot, job_id, phase
            self.session_id = session_id
            self.out, self.monitor, self.log, self.tool_index = out, monitor, log, 0
            mapping = cpu_mapping(slot, args.concurrency)
            self.cpus, self.node = mapping['cpus'], mapping['numa_node']
            self.image = image['image_id']
            self.name = f'str-{session_id}-{job_id}-{phase}'
            self.container_id, self.cgroup = None, None
            self.tool_timeout = getattr(args, 'tool_timeout', 300)
            self.trace = None
            self.config = type('Config', (), {'model_dump': lambda s, **kw: {
                'cpus': self.cpus, 'numa_node': self.node, 'memory_bytes': 4 * 1024**3}})()
            started, t0 = time.time(), time.monotonic()
            try:
                result = base.run(['docker', 'run', '-d', '--pull=never', '--name', self.name,
                    '--label', LABEL + '=true', '--label', LABEL + '.session=' + session_id,
                    '--cpus', '2', '--cpuset-cpus', self.cpus, '--cpuset-mems', str(self.node),
                    '--memory', '4g', '--memory-swap', '4g', '--pids-limit', '512',
                    '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '-w', '/testbed', self.image, 'sleep', '14400'], timeout=90)
                if result.returncode:
                    raise RuntimeError('container_start: ' + result.stderr[-1000:])
                self.container_id = result.stdout.strip()
                info = json.loads(base.run(['docker', 'inspect', self.container_id]).stdout)[0]
                self.pid = info['State']['Pid']
                cg = Path(f'/proc/{self.pid}/cgroup').read_text().strip().split('::')[1]
                self.cgroup = Path('/sys/fs/cgroup') / cg.lstrip('/')
                monitor.register(self.name, self.cgroup, self.pid, {
                    'job_id': job_id, 'instance_id': row['instance_id'], 'phase': phase,
                    'slot': slot, **mapping})
                initial = base.cg_snapshot(self.cgroup)
                log({'type': 'container_start', 'job_id': job_id, 'instance_id': row['instance_id'],
                     'phase': phase, 'slot': slot, 'name': self.name, 'container_id': self.container_id,
                     'start_ts': started, 'elapsed_s': time.monotonic() - t0,
                     'image_id': info['Image'], 'limits': initial, **mapping})
            except BaseException as error:
                # docker run may create a named container before a timeout/error.
                try:
                    if isinstance(error, subprocess.TimeoutExpired) and not self.container_id:
                        cleanup_uncertain_creation(base, self.name, self.session_id)
                    else:
                        remove_container(base, self.name, self.session_id)
                finally:
                    monitor.unregister(self.name)
                self.container_id = None
                raise

        def get_template_vars(self, **kwargs):
            return {'cwd': '/testbed', 'timeout': self.tool_timeout, **kwargs}

        def serialize(self):
            value = super().serialize()
            value['info']['config']['environment'].update(
                base_commit=self.row['base_commit'], tool_timeout=self.tool_timeout,
                environment=TOOL_ENV, network='none')
            return value

        def execute(self, action, cwd='', timeout=None, submit=True):
            timeout = self.tool_timeout if timeout is None else timeout
            tool_index = self.tool_index + 1
            request = {'type': 'tool_request', 'phase': self.phase, 'tool_index': tool_index,
                       'action': action, 'cwd': cwd or '/testbed', 'environment': TOOL_ENV,
                       'timeout_s': timeout, 'submit_enabled': submit}
            if self.trace:
                self.trace(request)
            result, error = None, None
            try:
                result = super().execute(action, cwd=cwd, timeout=timeout, submit=submit)
                return result
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                if self.trace:
                    output_path = self.out / f'{self.phase}-tool-{tool_index:03d}.log'
                    self.trace({'type': 'tool_result', 'phase': self.phase, 'tool_index': tool_index,
                                'result': result, 'exception_type': error,
                                'output_file': output_path.name if output_path.exists() else None,
                                'output_sha256': sha256(output_path) if output_path.exists() else None})

        def cleanup(self):
            if not self.container_id:
                return None
            resource = {}
            try:
                resource['cgroup'] = base.cg_snapshot(self.cgroup)
                inspected = base.run(['docker', 'inspect', self.container_id])
                resource['docker_state'] = json.loads(inspected.stdout)[0]['State'] if inspected.returncode == 0 else {}
                self.log({'type': 'container_final', 'job_id': self.job_id, 'phase': self.phase,
                          'snapshot': resource['cgroup'], 'state': resource['docker_state']})
            finally:
                try:
                    remove_container(base, self.name, self.session_id)
                    self.container_id = None
                finally:
                    self.monitor.unregister(self.name)
            return resource
    return FullEnvironment
