"""Standard-library validation for exported, sanitized trace bundles."""
from pathlib import Path
from .replay import ReplayValidationError, inside, read_json, sha256, validate_attempt
from runtime.full500_runtime import TOOL_ENV


def load_bundle(source, selection=None):
    source = Path(source).resolve()
    manifest_path = source / 'bundle-manifest.json'
    manifest = read_json(manifest_path) if manifest_path.exists() else None
    if manifest is not None:
        if manifest.get('schema_version') != 1 or manifest.get('kind') != 'sanitized-trace-bundle':
            raise ReplayValidationError('unsupported bundle manifest schema')
        entries = manifest.get('files', [])
        paths = [entry['path'] for entry in entries]
        if not entries or len(set(paths)) != len(paths):
            raise ReplayValidationError('manifest must list unique evidence files')
        for entry in entries:
            path = inside(source / entry['path'], source)
            if Path(entry['path']).is_absolute() or not path.is_file():
                raise ReplayValidationError('missing or unsafe manifest file: ' + entry['path'])
            if path.stat().st_size != entry['size_bytes'] or sha256(path) != entry['sha256']:
                raise ReplayValidationError('bundle SHA256/size mismatch: ' + entry['path'])
    if selection is not None:
        jobs = read_json(selection)['jobs']
    elif manifest is not None:
        jobs = manifest['tasks']
    else:
        # Frozen full-dataset exports may keep one final pointer per job.
        jobs = []
        for path in sorted((source / 'run').glob('*/result.json')):
            record = read_json(path)
            jobs.append({key: record[key] for key in ('job_id', 'instance_id', 'attempt_path')})
        if not jobs:
            for path in sorted((source / 'run').glob('*/attempt-*/result.json')):
                record = read_json(path)
                jobs.append({key: record[key] for key in ('job_id', 'instance_id', 'attempt_path')})
    if (not jobs or len({item['job_id'] for item in jobs}) != len(jobs)
            or len({item['instance_id'] for item in jobs}) != len(jobs)):
        raise ReplayValidationError('source must select unique job/instance IDs; use --selection to disambiguate attempts')
    if any(not str(item['job_id']).isascii() or not str(item['job_id']).isalnum() for item in jobs):
        raise ReplayValidationError('job IDs must be ASCII alphanumeric')
    validated = [validate_attempt(source, item) for item in jobs]
    for item in validated:
        for event in item['events']:
            if event['type'] == 'tool_request' and event['environment'] != TOOL_ENV:
                raise ReplayValidationError('recorded tool environment differs from runtime')
        if manifest is not None:
            evidence = [path.relative_to(source).as_posix() for path in item['attempt'].rglob('*') if path.is_file()]
            missing = set(evidence) - set(paths)
            if missing:
                raise ReplayValidationError('attempt files missing from manifest: ' + ', '.join(sorted(missing)))
    return validated, manifest
