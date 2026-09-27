#!/usr/bin/env python3
"""Replay saved SWE-bench tool actions in isolated Docker containers with zero model calls."""
from __future__ import annotations
import argparse
import concurrent.futures
import importlib.metadata
import json
import math
from pathlib import Path
import queue
import signal
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from engine.bundle import load_bundle
from engine.replay import atomic_json, load_frozen_runtime, replay_one, sha256
from runtime.preflight import docker_preflight, ensure_image
from runtime.topology import discover_topology, select_slots


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument('--source', type=Path, default=ROOT / 'examples/django-14672', help='trace bundle root (default: bundled example)')
    value.add_argument('--selection', type=Path, help='optional JSON jobs list to select frozen attempts')
    value.add_argument('--output', required=True, type=Path, help='new output directory outside source; required for every mode')
    value.add_argument('--wait-scale', type=float, default=1.0, help='recorded API-wait multiplier; 0 skips waits, never calls an API (default: 1)')
    value.add_argument('--concurrency', type=int, default=1, help='disjoint 2-CPU slots (default: 1)')
    value.add_argument('--cpus', help='optional available CPU pool, e.g. 0-7,16-23')
    value.add_argument('--numa-nodes', help='optional allowed NUMA node pool, e.g. 0,1')
    modes = value.add_mutually_exclusive_group()
    modes.add_argument('--validate-only', action='store_true', help='verify evidence with Python standard library only; no Docker or third-party imports')
    modes.add_argument('--prepare-image', action='store_true', help='verify/pull exact recorded images and exit without replay')
    value.add_argument('--image-reference', help='pull reference override for one-image preparation; final image ID must still match capture')
    return value


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if sys.version_info < (3, 11):
        cli.error('Python 3.11+ is required')
    if args.concurrency < 1 or not math.isfinite(args.wait_scale) or args.wait_scale < 0:
        cli.error('concurrency must be positive and wait-scale finite/nonnegative')
    if args.image_reference and not args.prepare_image:
        cli.error('--image-reference requires --prepare-image')
    source, output = args.source.resolve(), args.output.resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        cli.error('output must be separate from the captured source tree')
    if output.exists():
        cli.error('output must be a new directory')
    try:
        validated, manifest = load_bundle(source, args.selection)
        images = {item['image']['image_id']: item['image'] for item in validated}
        if args.image_reference and len(images) != 1:
            cli.error('--image-reference applies only to a selection with one unique image')
        validation = {'state': 'validated', 'source': str(source), 'jobs': len(validated),
                      'validated_events': sum(item['validated_events'] for item in validated),
                      'bundle_manifest_verified': manifest is not None,
                      'recorded_api_wait_seconds': sum(item['api_wait_seconds'] for item in validated),
                      'model_calls': 0, 'api_cost_rmb': 0,
                      'image_ids': sorted(images)}
        if args.validate_only:
            output.mkdir(parents=True)
            atomic_json(output / 'validation.json', validation)
            print(json.dumps(validation))
            return 0
        runtime = load_frozen_runtime()
        base, full = runtime
        docker = docker_preflight(base)
        image_checks = [ensure_image(base, image, args.prepare_image, args.image_reference) for image in images.values()]
        if args.prepare_image:
            output.mkdir(parents=True)
            report = {**validation, 'state': 'images_ready', 'images': image_checks, 'docker': docker}
            atomic_json(output / 'image-preparation.json', report)
            print(json.dumps(report))
            return 0
        # A missing/mismatched grader is detected before any container is created.
        if importlib.metadata.version('swebench') != '3.0.17':
            raise RuntimeError('replay requires the captured grader version swebench==3.0.17')
        from swebench.harness.grading import get_eval_report  # noqa: F401
        from swebench.harness.test_spec.test_spec import make_test_spec  # noqa: F401
        mappings = select_slots(discover_topology(), args.concurrency, args.cpus, args.numa_nodes)
        full.CONCURRENCY, full.SLOT_MAPPINGS = args.concurrency, mappings
        output.mkdir(parents=True)
        atomic_json(output / 'validation.json', validation)
        atomic_json(output / 'configuration.json', {
            'source': str(source), 'concurrency': args.concurrency, 'wait_scale': args.wait_scale,
            'slot_mappings': mappings, 'docker': docker, 'images': image_checks,
            'limits': {'cpus': 2, 'memory_bytes': 4 * 1024**3, 'swap_bytes': 0},
            'model_calls': 0, 'api_cost_rmb': 0, 'grader': 'swebench==3.0.17',
            'runtime_sources': {str(path.relative_to(ROOT)): sha256(path)
                                for folder in ('engine', 'runtime', 'scripts')
                                for path in sorted((ROOT / folder).glob('*.py'))},
        })
        return execute(args, validated, output, runtime)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f'replay: {type(error).__name__}: {error}', file=sys.stderr)
        return 1


def execute(args, validated, output, runtime):
    base, full = runtime
    log = base.Log(output / 'events.jsonl')
    monitor = base.Monitor(output / 'metrics.jsonl', interval=1).start()
    stop = threading.Event()
    full.STOP_EVENT = stop
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *unused: stop.set())
    pending = queue.Queue()
    for item in validated:
        pending.put(item)
    results, lock = [], threading.Lock()
    session_id = 'replay' + uuid.uuid4().hex[:12]
    started = time.time()

    def write_status():
        atomic_json(output / 'status.json', {'state': 'running', 'start_ts': started,
            'updated_ts': time.time(), 'selected': len(validated), 'finished': len(results),
            'session_id': session_id,
            'results': [{key: result.get(key) for key in ('job_id', 'instance_id', 'state', 'verdict', 'functional_equal', 'normalized_consistent')} for result in results]})

    def worker(slot):
        while not stop.is_set():
            try:
                item = pending.get_nowait()
            except queue.Empty:
                return
            result = replay_one(item, slot, output, runtime, monitor, log, args.wait_scale, stop, session_id)
            if result.get('cleanup_error') or result.get('error_type') in ('ContainerCleanupError', 'ContainerCreationUncertain'):
                stop.set()
            with lock:
                results.append(result)
                write_status()
            pending.task_done()

    write_status()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(worker, slot) for slot in range(args.concurrency)]
            for future in futures:
                future.result()
    finally:
        try:
            monitor.stop()
        finally:
            log.close()
    summary = {'state': 'completed' if len(results) == len(validated) and not stop.is_set() else 'interrupted',
        'session_id': session_id, 'start_ts': started, 'end_ts': time.time(),
        'selected': len(validated), 'finished': len(results),
        'errors': sum(result['state'] == 'error' for result in results),
        'cleanup_state': ('uncertain' if any(result.get('cleanup_error') or result.get('error_type')
            in ('ContainerCleanupError', 'ContainerCreationUncertain') for result in results) else 'confirmed'),
        'functional_equal': sum(bool(result.get('functional_equal')) for result in results),
        'normalized_consistent': sum(bool(result.get('normalized_consistent')) for result in results),
        'model_calls': 0, 'api_cost_rmb': 0, 'results': results}
    atomic_json(output / 'summary.json', summary)
    atomic_json(output / 'status.json', summary)
    print(json.dumps({key: value for key, value in summary.items() if key != 'results'}))
    # Preserve the original exit convention: successful execution may report
    # differences_found. Read summary/results for scientific agreement.
    return 1 if summary['errors'] or summary['state'] != 'completed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
