"""Local Docker preflight and immutable recorded-image preparation."""
import json
import os
from pathlib import Path
import platform


def docker_preflight(base):
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise RuntimeError('replay requires a local x86_64 Linux Docker host; validate-only works on other hosts')
    if not Path('/sys/fs/cgroup/cgroup.controllers').is_file():
        raise RuntimeError('replay requires Linux cgroup v2')
    # Docker CLI gives DOCKER_CONTEXT precedence over DOCKER_HOST.
    context_name = os.environ.get('DOCKER_CONTEXT')
    endpoint = None if context_name else os.environ.get('DOCKER_HOST')
    if not endpoint:
        command = ['docker', 'context', 'inspect']
        if context_name:
            command.append(context_name)
        context = base.run(command, timeout=30)
        if context.returncode:
            raise RuntimeError('cannot inspect Docker context: ' + context.stderr[-1000:])
        endpoint = json.loads(context.stdout)[0]['Endpoints']['docker']['Host']
    if not endpoint.startswith('unix://'):
        raise RuntimeError('replay requires a local Unix-socket Docker daemon so /proc and cgroups match the containers')
    info = base.run(['docker', 'info', '--format', '{{json .}}'], timeout=30)
    if info.returncode:
        raise RuntimeError('Docker daemon unavailable: ' + info.stderr[-1000:])
    data = json.loads(info.stdout)
    if data.get('OSType') != 'linux' or str(data.get('CgroupVersion')) != '2':
        raise RuntimeError('Docker daemon must use Linux and cgroup v2')
    if data.get('Architecture') not in ('x86_64', 'amd64'):
        raise RuntimeError('the recorded images require x86_64 Docker')
    if ['driver-type', 'io.containerd.snapshotter.v1'] not in (data.get('DriverStatus') or []):
        raise RuntimeError('this capture records manifest-based Docker image IDs and requires the containerd image store; classic graphdriver IDs are incompatible. See https://docs.docker.com/engine/storage/containerd/ ; no daemon settings were changed')
    return {key: data.get(key) for key in ('ServerVersion', 'OSType', 'Architecture', 'CgroupVersion', 'Driver', 'DriverStatus')}


def ensure_image(base, image, prepare=False, reference=None):
    expected = image['image_id']
    inspected = base.run(['docker', 'image', 'inspect', expected], timeout=30)
    pulled = None
    if inspected.returncode and prepare:
        candidates = [reference] if reference else [*(image.get('repo_digests') or []), image.get('source_image')]
        candidates = list(dict.fromkeys(value for value in candidates if value and not value.startswith('sha256:')))
        if not candidates:
            raise RuntimeError('image unavailable; use --prepare-image --image-reference REPOSITORY:TAG or docker load from a trusted archive')
        errors = []
        for candidate in candidates:
            fetched = base.run(['docker', 'pull', candidate], timeout=1800)
            if fetched.returncode:
                errors.append(candidate + ': ' + fetched.stderr[-500:])
                continue
            checked = base.run(['docker', 'image', 'inspect', candidate], timeout=30)
            if checked.returncode:
                errors.append(candidate + ': pulled image cannot be inspected')
                continue
            actual = json.loads(checked.stdout)[0]['Id']
            if actual != expected:
                raise RuntimeError(f'image ID drift: pulled {actual}, recorded {expected}; replay refused')
            inspected, pulled = checked, candidate
            break
        if inspected.returncode:
            raise RuntimeError('could not obtain recorded image; try --image-reference or docker load:\n' + '\n'.join(errors))
    if inspected.returncode:
        raise RuntimeError(f'recorded image {expected} is not cached; run --prepare-image first or docker load an exact image archive')
    info = json.loads(inspected.stdout)[0]
    if info['Id'] != expected:
        raise RuntimeError(f'image ID drift: cached {info["Id"]}, recorded {expected}')
    if info.get('Architecture') not in ('amd64', 'x86_64'):
        raise RuntimeError('recorded image architecture is not x86_64')
    return {'image_id': expected, 'pulled_reference': pulled, 'verified': True}
