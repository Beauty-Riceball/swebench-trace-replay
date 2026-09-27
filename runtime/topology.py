"""Discover usable Linux CPU/NUMA placement; no captured-host CPU numbers."""
from __future__ import annotations
import os
from pathlib import Path
import re


def parse_cpu_list(value):
    result = set()
    for part in value.split(','):
        if not re.fullmatch(r'\d+(?:-\d+)?', part):
            raise ValueError('CPU/node lists must look like 0,2,4-7')
        bounds = [int(item) for item in part.split('-')]
        first, last = bounds[0], bounds[-1]
        if last < first or last - first > 100000:
            raise ValueError('invalid CPU/node range')
        result.update(range(first, last + 1))
    return result


def discover_topology(sys_root=Path('/sys'), proc_root=Path('/proc'), affinity=None):
    root = Path(sys_root) / 'devices/system'
    allowed = set(os.sched_getaffinity(0) if affinity is None else affinity)
    online = root / 'cpu/online'
    if online.exists():
        allowed &= parse_cpu_list(online.read_text().strip())
    nodes = {}
    for cpulist in sorted((root / 'node').glob('node[0-9]*/cpulist')):
        node = int(cpulist.parent.name[4:])
        for cpu in parse_cpu_list(cpulist.read_text().strip()):
            nodes[cpu] = node
    memory_nodes = set(nodes.values()) or {0}
    status = Path(proc_root) / 'self/status'
    if status.exists():
        for line in status.read_text().splitlines():
            if line.startswith('Mems_allowed_list:'):
                memory_nodes &= parse_cpu_list(line.split(':', 1)[1].strip())
    result = []
    for cpu in sorted(allowed):
        directory = root / f'cpu/cpu{cpu}/topology'
        try:
            core = (int((directory / 'physical_package_id').read_text()),
                    int((directory / 'core_id').read_text()))
        except (OSError, ValueError):
            core = ('unknown', cpu)
        node = nodes.get(cpu, 0)
        if node in memory_nodes:
            result.append({'cpu': cpu, 'node': node, 'core': core})
    return result


def select_slots(topology, concurrency, cpus=None, numa_nodes=None):
    available = {item['cpu'] for item in topology}
    requested = available if cpus is None else parse_cpu_list(cpus)
    if not requested <= available:
        raise ValueError('requested CPUs are unavailable to this process: ' + str(sorted(requested - available)))
    nodes = {item['node'] for item in topology}
    selected_nodes = nodes if numa_nodes is None else parse_cpu_list(numa_nodes)
    if not selected_nodes <= nodes:
        raise ValueError('requested NUMA nodes are unavailable: ' + str(sorted(selected_nodes - nodes)))
    candidates = [item for item in topology if item['cpu'] in requested and item['node'] in selected_nodes]
    # Use one hardware thread per physical core, then form same-node pairs first.
    cores = {}
    for item in candidates:
        cores.setdefault(tuple(item['core']), item)
    remaining = sorted(cores.values(), key=lambda item: (item['node'], item['cpu']))
    if len(remaining) < 2 * concurrency:
        raise ValueError(f'need {2 * concurrency} available physical cores for {concurrency} disjoint slots; found {len(remaining)}; reduce --concurrency or expand --cpus')
    pairs = []
    for node in sorted(selected_nodes):
        local = [item for item in remaining if item['node'] == node]
        while len(local) >= 2:
            pair, local = local[:2], local[2:]
            pairs.append(pair)
            for item in pair:
                remaining.remove(item)
    while len(remaining) >= 2:
        pairs.append(remaining[:2])
        remaining = remaining[2:]
    return [{'cpus': ','.join(str(item['cpu']) for item in pair),
             'numa_node': ','.join(str(node) for node in sorted({item['node'] for item in pair})),
             'physical_pair': index}
            for index, pair in enumerate(pairs[:concurrency])]
