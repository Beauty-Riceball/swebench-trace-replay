#!/usr/bin/env python3
"""Derived from the original replay_sample.py; executes its real replay_one path.

The host-specific loader/slot configuration is adapted for this portable export.
This file is derived code, not a byte-identical frozen capture artifact.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time
import traceback
from types import SimpleNamespace

from .replay_compare import compare_eval, compare_output


class ReplayValidationError(RuntimeError):
    pass


class ReplayStopped(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def inside(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise ReplayValidationError(f'path is outside source root: {path}')
    return path


def validate_attempt(source_root, job):
    """Validate immutable recorded evidence before any container is created."""
    source_root = Path(source_root).resolve()
    attempt = Path(job['attempt_path'])
    if not attempt.is_absolute():
        attempt = source_root / attempt
    attempt = inside(attempt, source_root)
    result_path = attempt / 'result.json'
    if not result_path.exists():
        result_path = attempt.parent / 'result.json'
    record = read_json(inside(result_path, source_root))
    if record.get('job_id') != job['job_id'] or record.get('instance_id') != job['instance_id']:
        raise ReplayValidationError('job identity differs from recorded result')
    if not record.get('capture_complete') or record.get('state') != 'completed':
        raise ReplayValidationError('selected attempt is not a complete capture')
    if Path(record['attempt_path']).name != attempt.name:
        raise ReplayValidationError('selected attempt differs from final result pointer')
    trajectory = inside(attempt / 'trajectory.json', attempt)
    if sha256(trajectory) != record.get('trajectory_sha256'):
        raise ReplayValidationError('trajectory SHA256 mismatch')
    events = []
    pending = {}
    results = {}
    next_index = {}
    api_wait_seconds = 0.0
    api_responses = 0
    for i, file in enumerate(sorted((attempt / 'trace').glob('*.json')), start=1):
        event = read_json(inside(file, attempt))
        if event.get('sequence') != i or not file.name.startswith(f'{i:06d}-'):
            raise ReplayValidationError('trace sequence is missing, duplicated, or out of order')
        kind = event.get('type')
        if kind == 'tool_request':
            phase = event.get('phase')
            if phase not in {'agent', 'eval'}:
                raise ReplayValidationError('unexpected tool phase')
            key = (phase, event['tool_index'])
            if event['tool_index'] != next_index.get(phase, 1) or pending:
                raise ReplayValidationError('tool request pairing or index mismatch')
            next_index[phase] = event['tool_index'] + 1
            pending[key] = event
            timeout = event.get('timeout_s')
            if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ReplayValidationError('invalid recorded tool timeout')
            if not isinstance(event.get('action', {}).get('command'), str):
                raise ReplayValidationError('missing recorded command')
        elif kind == 'tool_result':
            key = (event.get('phase'), event.get('tool_index'))
            if key not in pending or key in results:
                raise ReplayValidationError('unpaired or duplicated tool result')
            del pending[key]
            output_name = event.get('output_file')
            if not output_name or Path(output_name).name != output_name:
                raise ReplayValidationError('missing or unsafe tool output filename')
            output_path = inside(attempt / output_name, attempt)
            if sha256(output_path) != event.get('output_sha256'):
                raise ReplayValidationError('tool output SHA256 mismatch')
            expected = event.get('result')
            if expected is not None and expected.get('output') != output_path.read_text():
                raise ReplayValidationError('tool output journal and output file differ')
            if expected is None and event.get('exception_type') != 'Submitted':
                raise ReplayValidationError('unsupported captured tool exception')
            results[key] = event
        elif kind == 'api_response':
            wait = event.get('elapsed_seconds')
            if not isinstance(wait, (int, float)) or not math.isfinite(wait) or wait < 0:
                raise ReplayValidationError('invalid recorded API wait duration')
            api_wait_seconds += wait
            api_responses += 1
        events.append(event)
    if pending or len(events) != record.get('trace_events_saved'):
        raise ReplayValidationError('trace event count or tool pairing mismatch')
    if api_responses != record.get('api_responses_saved'):
        raise ReplayValidationError('API response count mismatch')
    task = read_json(inside(attempt / 'task.json', attempt))
    environment = read_json(inside(attempt / 'agent-environment.json', attempt))
    if task['instance_id'] != job['instance_id'] or environment['instance_id'] != job['instance_id']:
        raise ReplayValidationError('task/environment identity mismatch')
    if environment.get('limits') != {'cpus': 2, 'memory_bytes': 4 * 1024**3, 'swap_bytes': 0}:
        raise ReplayValidationError('recorded resource limits differ from 2 CPU / 4 GiB')
    image = environment['image']
    if not str(image.get('image_id', '')).startswith('sha256:'):
        raise ReplayValidationError('recorded image lacks immutable image ID')
    provided_image = job.get('image')
    if provided_image:
        provided_id = provided_image.get('image_id', provided_image.get('image')) if isinstance(provided_image, dict) else provided_image
        if provided_id != image['image_id']:
            raise ReplayValidationError('selection image differs from recorded image ID')
    patch_path = inside(attempt / 'prediction.patch', attempt)
    if not patch_path.exists():
        raise ReplayValidationError('recorded prediction.patch is missing')
    if any(event.get('phase') == 'eval' and event['type'] == 'tool_request'
           and '/eval.sh' in event['action']['command'] for event in events):
        if not inside(attempt / 'eval.sh', attempt).is_file():
            raise ReplayValidationError('recorded evaluation script is missing')
    eval_environment_path = inside(attempt / 'eval-environment.json', attempt)
    if eval_environment_path.exists():
        eval_environment = read_json(eval_environment_path)
        if (eval_environment.get('instance_id') != job['instance_id']
                or eval_environment.get('limits') != environment['limits']
                or eval_environment.get('image', {}).get('image_id') != image['image_id']):
            raise ReplayValidationError('evaluation image or resource limits differ from agent environment')
    return {'job': job, 'attempt': attempt, 'record': record, 'task': task,
            'environment': environment, 'image': image, 'events': events,
            'tool_results': results, 'original_patch_sha256': sha256(patch_path),
            'api_wait_seconds': api_wait_seconds, 'validated_events': len(events)}


def forbidden_paid_operation(*args, **kwargs):
    raise RuntimeError('model construction and credential access are disabled during replay')


def load_frozen_runtime(source_root=None):
    """Load the derived replay-only runtime, never the capture/model modules."""
    from runtime import runner, full500_runtime
    runner.read_key = forbidden_paid_operation
    runner.DeepSeekModel = forbidden_paid_operation
    return runner, full500_runtime


def parse_submission(output):
    lines = output.lstrip().splitlines(keepends=True)
    if not lines or lines[0].strip() != 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT':
        raise ReplayValidationError('Submitted exception lacks recorded sentinel in actual stdout')
    return ''.join(lines[1:])


def verify_container_image(base, environment, image_id):
    response = base.run(['docker', 'inspect', environment.container_id], timeout=30)
    if response.returncode:
        raise RuntimeError('cannot inspect newly created replay container: ' + response.stderr[-1000:])
    actual = json.loads(response.stdout)[0]['Image']
    if actual != image_id:
        raise ReplayValidationError(f'created container image ID mismatch: {actual} != {image_id}')
    return actual


def execute_recorded_tool(env, request, expected, attempt, out, base):
    """Execute one actual command and compare it against its persisted result."""
    started = time.monotonic()
    actual_exception = None
    submission = None
    try:
        result = env.execute(request['action'], cwd=request['cwd'],
                             timeout=request['timeout_s'], submit=request['submit_enabled'])
    except base.Submitted:
        actual_exception = 'Submitted'
        stdout_file = out / f"{request['phase']}-tool-{request['tool_index']:03d}.log"
        output = stdout_file.read_text()
        submission = parse_submission(output)
        result = {'output': output, 'returncode': 0, 'exception_info': ''}
    expected_result = expected.get('result')
    expected_output = (attempt / expected['output_file']).read_text()
    expected_rc = expected_result['returncode'] if expected_result else 0
    output_comparison = compare_output(expected_output, result['output'])
    comparison = {
        'sequence': request['sequence'], 'phase': request['phase'], 'tool_index': request['tool_index'],
        'command': request['action']['command'], 'cwd': request['cwd'], 'timeout_s': request['timeout_s'],
        'elapsed_s': time.monotonic() - started,
        'expected_returncode': expected_rc, 'actual_returncode': result['returncode'],
        'returncode_equal': expected_rc == result['returncode'],
        'expected_exception': expected.get('exception_type'), 'actual_exception': actual_exception,
        'exception_equal': expected.get('exception_type') == actual_exception,
        'exception_info_equal': (expected_result or {}).get('exception_info', '') == result.get('exception_info', ''),
        'expected_output_sha256': expected['output_sha256'],
        'actual_output_sha256': hashlib.sha256(result['output'].encode()).hexdigest(),
        **output_comparison,
    }
    # Docker transport failures are infrastructure failures, while a shell's
    # nonzero exit code is an ordinary recorded workload outcome.
    if result['output'].startswith(('Error response from daemon:', 'Cannot connect to the Docker daemon')):
        raise RuntimeError('Docker transport failure: ' + result['output'][:1000])
    return comparison, result, submission


def replay_one(validated, slot, out, runtime, monitor, log, wait_scale, stop, session_id):
    base, full = runtime
    Env = full.make_environment_class(base)
    args = SimpleNamespace(concurrency=getattr(full, 'CONCURRENCY', 1), tool_timeout=300)
    job, original, task = validated['job'], validated['record'], validated['task']
    attempt = validated['attempt']
    output = Path(out) / job['job_id']
    output.mkdir()
    record = {'job_id': job['job_id'], 'instance_id': job['instance_id'], 'slot': slot,
              'state': 'running', 'start_ts': time.time(), 'wait_scale': wait_scale,
              'model_calls': 0, 'api_cost_rmb': 0, 'source_attempt': str(attempt),
              'original_exit_status': original['exit_status'], 'source_validation_passed': True,
              'validated_events': validated['validated_events'], 'tool_comparisons': [],
              'api_waits': [], 'initial_source_comparisons': [], 'images': {}}
    atomic_json(output / 'status.json', record)
    env = None
    current_phase = None
    submission = None
    final_diff = None
    patch = None
    seen_api = False
    eval_script_copied = False
    last_eval_result = None
    eval_apply_returncode = None
    try:
        for event in validated['events']:
            if stop.is_set():
                raise ReplayStopped('replay stopped')
            if event['type'] == 'api_request':
                seen_api = True
            if event['type'] == 'api_response':
                requested = event['elapsed_seconds'] * wait_scale
                started = time.monotonic()
                if stop.wait(requested):
                    raise ReplayStopped('replay stopped during recorded API wait')
                record['api_waits'].append({'sequence': event['sequence'],
                    'recorded_s': event['elapsed_seconds'], 'requested_s': requested,
                    'actual_s': time.monotonic() - started})
                continue
            if event['type'] != 'tool_request':
                continue
            if event['environment'] != full.TOOL_ENV:
                raise ReplayValidationError('recorded tool environment differs from frozen runtime')
            phase = event['phase']
            if phase != current_phase:
                if phase == 'eval':
                    patch = submission if submission else final_diff
                    if patch is None:
                        raise ReplayValidationError('agent replay produced neither submission nor final working-tree diff')
                    (output / 'prediction.patch').write_text(patch)
                if env:
                    record[current_phase + '_resources'] = env.cleanup()
                    env = None
                env = Env(task, slot, job['job_id'], phase, output, monitor, log,
                          args, validated['image'], session_id)
                current_phase = phase
                record['images'][phase] = verify_container_image(base, env, validated['image']['image_id'])
                if phase == 'agent':
                    # Original runner waits two seconds after container startup.
                    if stop.wait(2):
                        raise ReplayStopped('replay stopped during startup wait')
                else:
                    env.copy(output / 'prediction.patch', '/tmp/prediction.patch')
            if phase == 'eval' and not eval_script_copied and '/eval.sh' in event['action']['command']:
                env.copy(attempt / 'eval.sh', '/eval.sh')
                eval_script_copied = True
            comparison, result, submitted_patch = execute_recorded_tool(
                env, event, validated['tool_results'][(phase, event['tool_index'])], attempt, output, base)
            record['tool_comparisons'].append(comparison)
            atomic_json(output / f"compare-{phase}-{event['tool_index']:03d}.json", comparison)
            if phase == 'agent':
                if not seen_api:
                    record['initial_source_comparisons'].append({
                        'tool_index': event['tool_index'], 'raw_equal': comparison['raw_equal'],
                        'returncode_equal': comparison['returncode_equal']})
                if submitted_patch is not None:
                    submission = submitted_patch
                if not event['submit_enabled'] and event['action']['command'] == 'git diff --binary':
                    final_diff = result['output']
            else:
                if '/eval.sh' in event['action']['command']:
                    last_eval_result = result
                if '/tmp/prediction.patch' in event['action']['command']:
                    eval_apply_returncode = result['returncode']
            atomic_json(output / 'status.json', {**record, 'updated_ts': time.time()})
        if patch is None:
            patch = submission if submission else final_diff
            if patch is None:
                raise ReplayValidationError('missing actual replay patch')
            (output / 'prediction.patch').write_text(patch)
        record['patch_source'] = 'replayed_submission' if submission else 'replayed_working_tree_diff'
        record['patch_sha256'] = sha256(output / 'prediction.patch')
        record['original_patch_sha256'] = validated['original_patch_sha256']
        record['patch_equal'] = record['patch_sha256'] == validated['original_patch_sha256']
        if last_eval_result is not None:
            (output / 'test_output.txt').write_text(last_eval_result['output'])
            spec = base.make_test_spec(task)
            prediction = {'instance_id': job['instance_id'], 'model_name_or_path': 'deepseek-flash', 'model_patch': patch}
            grader_report = base.get_eval_report(spec, prediction, output / 'test_output.txt', include_tests_status=True)
            evaluation = grader_report[job['instance_id']]
            evaluation['eval_returncode'] = last_eval_result['returncode']
            if eval_apply_returncode:
                evaluation['error'] = 'patch_apply_failed'
            atomic_json(output / 'eval_report.json', grader_report)
        elif eval_apply_returncode:
            evaluation = {'resolved': False, 'error': 'patch_apply_failed'}
        elif not patch.strip():
            evaluation = {'resolved': False, 'error': 'empty_patch'}
        else:
            evaluation = {'resolved': False, 'error': 'no_recorded_evaluation_actions'}
        record['evaluation'] = evaluation
        record['eval_comparison'] = compare_eval(original['evaluation'], evaluation)
        tools = record['tool_comparisons']
        record['counts'] = {name: sum(item[name] for item in tools) for name in (
            'raw_equal', 'normalized_equal', 'returncode_equal', 'exception_equal', 'exception_info_equal')}
        record['counts']['tools'] = len(tools)
        record['initial_source_equal'] = bool(record['initial_source_comparisons']) and all(
            item['raw_equal'] and item['returncode_equal'] for item in record['initial_source_comparisons'])
        record['rc_exception_equal'] = all(item['returncode_equal'] and item['exception_equal'] and item['exception_info_equal'] for item in tools)
        record['functional_equal'] = record['initial_source_equal'] and record['patch_equal'] and record['eval_comparison']['equal'] and record['rc_exception_equal']
        record['normalized_consistent'] = record['initial_source_equal'] and record['functional_equal'] and all(item['normalized_equal'] for item in tools)
        record['state'] = 'completed'
        record['verdict'] = 'consistent' if record['normalized_consistent'] else 'differences_found'
    except Exception as error:
        record.update(state='error', error_type=type(error).__name__, error=str(error))
        (output / 'error.txt').write_text(traceback.format_exc())
    finally:
        if env:
            try:
                record[current_phase + '_resources'] = env.cleanup()
            except Exception as error:
                record.update(state='error', cleanup_error=str(error))
        record['end_ts'] = time.time()
        record['elapsed_s'] = record['end_ts'] - record['start_ts']
        atomic_json(output / 'result.json', record)
        atomic_json(output / 'status.json', record)
    return record
