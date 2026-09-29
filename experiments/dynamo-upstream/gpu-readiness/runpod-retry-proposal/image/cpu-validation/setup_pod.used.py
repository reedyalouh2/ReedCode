"""Check capture permissions, install isolated tools, and start the parity server."""

import argparse
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4

DEPS = ('httpx==0.28.1', 'msgspec==0.21.1', 'pyzmq==27.1.0',
        'msgpack==1.1.1', 'xxhash==3.5.0')


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def command(argv, output, timeout=120):
    with output.open('w') as log:
        subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=timeout)


def packages():
    return {dist.metadata['Name']:dist.version for dist in metadata.distributions()}


def capture_check(directory):
    executable = shutil.which('tcpdump')
    if executable is None:
        if os.geteuid() != 0:
            raise RuntimeError('tcpdump is missing and this process is not root; stop before model download')
        command(['apt-get', 'update'], directory/'apt-update.log')
        command(['apt-get', 'install', '-y', '--no-install-recommends', 'tcpdump'], directory/'apt-install.log')
        executable = shutil.which('tcpdump')
    if executable is None:
        raise RuntimeError('tcpdump installation did not provide an executable')
    command([executable, '--version'], directory/'tcpdump-version.txt')
    log_path, pcap = directory/'capture.log', directory/'capture.pcap'
    with socket.socket() as listener, socket.socket() as other_port:
        listener.settimeout(3)
        listener.bind(('127.0.0.1',20000)); listener.listen(1)
        other_port.bind(('127.0.0.1',20003))
        argv = [executable, '--immediate-mode', '-i', 'lo', '-s', '0', '-U',
                '-B', '4096', '-w', str(pcap),
                'tcp', 'and', '(', 'port', '20000', 'or', 'port', '20003', ')']
        with log_path.open('w') as log:
            child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic()+8
                while 'listening on lo' not in log_path.read_text():
                    if child.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('Loopback capture could not start; see capture.log')
                    time.sleep(.05)
                payload = b'reedcode-local-capture-check\n'
                with socket.create_connection(('127.0.0.1',20000),timeout=3) as client:
                    connection, _ = listener.accept()
                    with connection:
                        client.sendall(payload)
                        if connection.recv(1024) != payload:
                            raise RuntimeError('Local capture-check payload mismatch')
                time.sleep(.25)
            finally:
                if child.poll() is None:
                    child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait(timeout=3)
                    raise RuntimeError('Capture did not stop after SIGINT')
    log = log_path.read_text()
    captured = re.search(r'(\d+) packets captured',log)
    dropped = re.search(r'(\d+) packets dropped by kernel',log)
    if (child.returncode or captured is None or int(captured[1]) == 0 or dropped is None
            or int(dropped[1]) != 0 or pcap.stat().st_size <= 24):
        raise RuntimeError('Loopback capture did not produce complete packets')
    command([executable, '-nn', '-r', str(pcap), '-A'],directory/'capture-decoded.txt')
    if 'reedcode-local-capture-check' not in (directory/'capture-decoded.txt').read_text():
        raise RuntimeError('Captured packets do not contain the local test payload')
    result = {'passed':True,'packets_captured':int(captured[1]),'packets_dropped':int(dropped[1]),
              'command':argv,'payload':'reedcode-local-capture-check','model_download_started':False}
    save(directory/'capture-result.json',result)
    return result


def run(args):
    if not args.execute:
        return {'execute':False,'bundle':str(args.bundle),'study_dir':str(args.study_dir),
                'control_venv':str(args.control_venv),'control_dependencies':DEPS,
                'note':'No install, capture, model download, or process launch performed.'}
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Run only in the approved Linux x86_64 study container')
    for name in ('bundle','study_dir','control_venv'):
        setattr(args,name,getattr(args,name).resolve())
    base = args.bundle/'repo/experiments/dynamo-upstream/001-speculative-prefill/linux-readiness'
    collector = args.bundle/'repo/experiments/dynamo-upstream/gpu-readiness/collect_kv.py'
    for path in (base/'setup-frontends.sh',base/'launch-on-pod.sh',base/'fresh_frontend.py',collector,args.bundle/'wheels/artifact-manifest.json'):
        if not path.is_file():raise ValueError('Missing bundle file: '+str(path))
    if args.control_venv.exists() or (args.study_dir/'bootstrap').exists():
        raise ValueError('Bootstrap paths already exist; inspect saved state before retrying')
    directory = args.study_dir/'bootstrap'
    directory.mkdir(parents=True)
    before = packages(); save(directory/'base-packages-before.json',before)
    if any(metadata.version(name)!=version for name,version in (('ai-dynamo','1.5.0'),('ai-dynamo-runtime','1.5.0'),('vllm','0.28.0'))):
        raise RuntimeError('The base image package versions differ from the pinned deployment')
    command(['id'],directory/'id.txt')
    status = {line.split(':',1)[0]:line.split(':',1)[1].strip()
              for line in Path('/proc/self/status').read_text().splitlines()
              if line.startswith(('Uid:','Gid:','CapInh:','CapPrm:','CapEff:','CapBnd:','CapAmb:','NoNewPrivs:'))}
    save(directory/'identity.json',{'euid':os.geteuid(),'status':status,'python':sys.executable,'python_version':sys.version})
    command(['nvidia-smi','--query-gpu=name,driver_version,memory.total','--format=csv,noheader'],directory/'gpu.txt')
    command(['nvidia-smi'],directory/'nvidia-smi.txt')
    capture_check(directory)
    command([sys.executable,'-m','venv',str(args.control_venv)],directory/'control-venv.log')
    control_python = args.control_venv/'bin/python'
    command([str(control_python),'-m','pip','install',*DEPS],directory/'control-install.log',timeout=180)
    command([str(control_python),'-m','pip','freeze'],directory/'control-packages.txt')
    command(['bash',str(base/'setup-frontends.sh'),str(args.bundle/'wheels'),str(args.study_dir)],directory/'frontend-setup.log',timeout=180)
    after=packages(); save(directory/'base-packages-after.json',after)
    if before!=after:raise RuntimeError('Base image Python packages changed during setup')
    spec=importlib.util.spec_from_file_location('fresh_frontend',base/'fresh_frontend.py')
    hook=importlib.util.module_from_spec(spec);spec.loader.exec_module(hook)
    parity=args.study_dir/'parity';parity.mkdir(exist_ok=True)
    epoch=uuid4().hex; states={}; children=[]
    try:
        observer_commands = (
            ('capture', [shutil.which('tcpdump'), '--immediate-mode', '-i', 'lo', '-s', '0',
                         '-U', '-B', '4096', '-w', str(parity/'wire.pcap'),
                         'tcp', 'and', '(', 'port', '20000', 'or', 'port', '20003', ')'],
             parity/'wire-capture.log'),
            ('kv', [str(control_python), str(collector), '--worker-epoch', epoch,
                    '--output', str(parity/'kv')], parity/'kv-collector.log'),
        )
        for component, argv, stdout in observer_commands:
            with stdout.open('wb') as output:
                child=subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            ticks=hook.start_ticks(child.pid)
            if ticks is None:raise RuntimeError(component+' observer exited immediately')
            children.append((child,ticks))
            states[component]={'pid':child.pid,'start_ticks':ticks,'epoch':epoch,
                               'started_unix':time.time(),'stdout_path':str(stdout),'component':component,
                               'command':argv}
            save(parity/'processes.json',states)
            deadline=time.monotonic()+8
            while True:
                if child.poll() is not None:raise RuntimeError(component+' observer exited before readiness')
                if component=='capture' and 'listening on lo' in stdout.read_text():break
                if component=='kv' and (parity/'kv/frames.jsonl').exists():break
                if time.monotonic()>deadline:raise TimeoutError(component+' observer did not become ready')
                time.sleep(.05)
        for component in ('backend','image'):
            stdout=parity/f'{component}-{epoch}.log'
            with stdout.open('wb') as output:
                child=subprocess.Popen(['bash',str(base/'launch-on-pod.sh'),str(args.study_dir),'parity',component,epoch],
                                       stdout=output,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
            ticks=hook.start_ticks(child.pid)
            if ticks is None:raise RuntimeError(component+' exited immediately')
            children.append((child,ticks))
            state={'pid':child.pid,'start_ticks':ticks,'epoch':epoch,'started_unix':time.time(),'stdout_path':str(stdout),'component':component}
            states[component]=state
            save(parity/'processes.json',states)
            if component=='image':
                save(parity/'frontend-state.json',{'frontend_pid':child.pid,'frontend_start_ticks':ticks,
                     'frontend_epoch':epoch,'started_unix':state['started_unix'],'stdout_path':str(stdout),'status':'starting'})
        deadline=time.monotonic()+args.wait_seconds
        while True:
            if any(child.poll() is not None for child,_ in children):raise RuntimeError('Parity server exited; inspect component logs')
            try:
                with urlopen('http://127.0.0.1:8000/v1/models',timeout=2) as response:models=json.load(response)
                if any(row.get('id')=='Qwen/Qwen3-8B' for row in models.get('data',[])):break
            except (OSError,ValueError):pass
            if time.monotonic()>deadline:raise TimeoutError('Parity model discovery timed out')
            time.sleep(1)
        result={'status':'ready_for_live_gates','capture_check_passed':True,'processes':states,
                'control_python':str(control_python),'model_response_verified':False,
                'engine_allocation_verified':False,'kv_reset_verified':False}
        frontend_state=json.loads((parity/'frontend-state.json').read_text())
        frontend_state['status']='ready_for_live_gates'
        save(parity/'frontend-state.json',frontend_state)
        save(directory/'result.json',result)
        return result
    except Exception as error:
        for child,ticks in reversed(children):hook.terminate_owned(child.pid,ticks,grace=1)
        save(directory/'result.json',{'status':'failed','error':str(error),'processes':states})
        raise


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle',type=Path,default=Path('/tmp/reedcode-bundle'))
    p.add_argument('--study-dir',type=Path,default=Path('/tmp/reedcode-study'))
    p.add_argument('--control-venv',type=Path,default=Path('/tmp/reedcode-observer-venv'))
    p.add_argument('--wait-seconds',type=int,default=900)
    p.add_argument('--execute',action='store_true')
    return p


if __name__=='__main__':
    args=parser().parse_args()
    if not 1<=args.wait_seconds<=1200:raise SystemExit('--wait-seconds must be between 1 and 1200')
    print(json.dumps(run(args)))
