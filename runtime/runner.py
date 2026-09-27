"""Replay-only extraction of the frozen runner (derived, not original bytes).

The execute/copy implementation retains Docker command, timeout, stdout, and
submission semantics. Capture/model imports and credential loading are removed.
"""
from __future__ import annotations
import json
import subprocess
import threading
import time
from pathlib import Path
from .monitor import Monitor


class Submitted(Exception):
    """Same submission signal consumed by replay_one; no agent SDK is needed."""


def forbidden_paid_operation(*args, **kwargs):
    raise RuntimeError('model construction and credential access are disabled during replay')


read_key = forbidden_paid_operation
DeepSeekModel = forbidden_paid_operation


def make_test_spec(*args, **kwargs):
    from swebench.harness.test_spec.test_spec import make_test_spec as implementation
    return implementation(*args, **kwargs)


def get_eval_report(*args, **kwargs):
    from swebench.harness.grading import get_eval_report as implementation
    return implementation(*args, **kwargs)


def run(argv, timeout=60, **kw):
    return subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=timeout, **kw)


def cg_snapshot(path):
    result = {}
    for name in ['cpu.stat','memory.current','memory.peak','memory.events','memory.stat',
                 'memory.swap.current','memory.numa_stat','io.stat','cpu.pressure','memory.pressure','io.pressure',
                 'cpu.max','cpuset.cpus.effective','cpuset.mems.effective','memory.max','memory.swap.max']:
        try: result[name] = (path/name).read_text().strip()
        except OSError: result[name] = None
    return result


class Log:
    def __init__(self, path):
        self.path=path; self.lock=threading.Lock()
        self.file=path.open('a', buffering=1)
    def __call__(self, event):
        event={'ts':time.time(),'monotonic_ns':time.monotonic_ns(), **event}
        with self.lock: self.file.write(json.dumps(event,ensure_ascii=False)+'\n')
    def close(self): self.file.close()


class Environment:
    def serialize(self):
        return {'info':{'config':{'environment':{'image':self.image,'cwd':'/testbed','cpus':self.cpus,'numa_node':self.node,'memory_bytes':4*1024**3,'swap_bytes':0}}}}


    def execute(self, action, cwd='', timeout=120, submit=True):
        command=action.get('command',''); self.tool_index+=1
        t0=time.monotonic(); started=time.time(); before=cg_snapshot(self.cgroup)
        self.log({'type':'tool_begin','job_id':self.job_id,'phase':self.phase,'tool_index':self.tool_index,'command':command})
        argv=['docker','exec','-w',cwd or '/testbed']
        for key,value in {'BASH_ENV':'/root/.bashrc','PAGER':'cat','PIP_PROGRESS_BAR':'off','TQDM_DISABLE':'1','OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'2','MKL_NUM_THREADS':'2'}.items():
            argv += ['-e',key+'='+value]
        argv += [self.container_id,'timeout','--signal=TERM','--kill-after=5s',str(timeout),'bash','-c',command]
        try:
            p=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace',timeout=timeout+20)
            result={'output':p.stdout,'returncode':p.returncode,'exception_info':''}
        except subprocess.TimeoutExpired:
            result={'output':'','returncode':124,'exception_info':'Host-side execution timeout'}
        after=cg_snapshot(self.cgroup)
        record={'type':'tool_end','job_id':self.job_id,'phase':self.phase,'instance_id':self.row['instance_id'],
                'tool_index':self.tool_index,'command':command,'start_ts':started,'elapsed_s':time.monotonic()-t0,
                'returncode':result['returncode'],'output_chars':len(result['output']),'before':before,'after':after}
        self.log(record)
        (self.out/f'{self.phase}-tool-{self.tool_index:03d}.log').write_text(result['output'])
        lines=result['output'].lstrip().splitlines(keepends=True)
        if submit and lines and lines[0].strip()=='COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' and result['returncode']==0:
            raise Submitted({'role':'exit','content':''.join(lines[1:]),'extra':{'exit_status':'Submitted','submission':''.join(lines[1:])}})
        return result


    def copy(self, source, target):
        p=run(['docker','cp',str(source),self.container_id+':'+target])
        if p.returncode: raise RuntimeError('docker cp failed: '+p.stderr[-500:])
