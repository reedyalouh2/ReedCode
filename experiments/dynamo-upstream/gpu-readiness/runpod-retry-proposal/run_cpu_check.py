"""Run the root-user capture gate in the immutable image without a GPU or host mounts."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parent
PARENT='nvcr.io/nvidia/ai-dynamo/vllm-runtime@sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image',default=PARENT)
    p.add_argument('--configured-root',action='store_true',help='Require the image config itself to select root')
    p.add_argument('--execute',action='store_true')
    args=p.parse_args()
    if not args.execute:
        print(json.dumps({'execute':False,'image':args.image,'user_override':None if args.configured_root else '0'}));return
    directory=ROOT/'image'/('derived-cpu-validation' if args.configured_root else 'cpu-validation')
    directory.mkdir(exist_ok=True)
    output=directory/'container-check'
    if output.exists():raise ValueError('Choose an unused validation output')
    inspect=json.loads(subprocess.check_output(['docker','image','inspect',args.image]))[0]
    prefix='' if args.configured_root else 'parent-'
    expected_raw=(ROOT/'image'/(prefix+'manifest.json')).read_bytes()
    expected_digest='sha256:'+hashlib.sha256(expected_raw).hexdigest()
    if not args.image.endswith('@'+expected_digest):
        raise ValueError('Use the exact verified manifest digest')
    expected_config=json.loads((ROOT/'image'/(prefix+'config.json')).read_text())
    if inspect['Config']!=expected_config['config'] or inspect['RootFS']['Layers']!=expected_config['rootfs']['diff_ids']:
        raise ValueError('Docker image config or filesystem layers differ from the verified metadata')
    if args.configured_root and inspect['Config'].get('User')!='0':raise ValueError('Derived image does not select USER0')
    (directory/'image-inspect.json').write_text(json.dumps(inspect,indent=2)+'\n')
    helper=(ROOT.parent/'setup_pod.py').read_text()
    (directory/'setup_pod.used.py').write_text(helper)
    program='''import hashlib, importlib.metadata as metadata, json, os, pathlib, sys, types
folder=pathlib.Path('/tmp/reedcode-cpu-check');folder.mkdir()
identity={'euid':os.geteuid(),'status':{line.split(':',1)[0]:line.split(':',1)[1].strip() for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith(('Uid:','Gid:','CapEff:','CapBnd:','NoNewPrivs:'))}}
(folder/'identity.json').write_text(json.dumps(identity,indent=2))
assert os.geteuid()==0,identity
assert int(identity['status']['CapEff'],16)&(1<<13),identity
before={d.metadata['Name']:d.version for d in metadata.distributions()}
assert metadata.version('ai-dynamo-runtime')=='1.5.0'
assert metadata.version('ai-dynamo')=='1.5.0'
assert metadata.version('vllm')=='0.28.0'
(folder/'base-packages-before.json').write_text(json.dumps(before,indent=2))
helper=types.ModuleType('capture_helper')
exec(compile(HELPER_SOURCE,'setup_pod.py','exec'),helper.__dict__)
result=helper.capture_check(folder)
after={d.metadata['Name']:d.version for d in metadata.distributions()}
(folder/'base-packages-after.json').write_text(json.dumps(after,indent=2))
assert before==after,'Base Python packages changed'
summary={'passed':True,'identity':identity,'capture':result,'base_python_packages_unchanged':True,'gpu_used':False,'host_mounts':False,'network_use':'Public apt repositories and own loopback test only'}
(folder/'result.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary),flush=True)
'''.replace('HELPER_SOURCE',repr(helper))
    name='reedcode-user0-cpu-'+uuid4().hex[:10]
    argv=['docker','run','--name',name,'--platform','linux/amd64','--cpus','2','--memory','3g','--pids-limit','256']
    if not args.configured_root:argv+=['--user','0']
    argv += ['--entrypoint','python3',args.image,'-u','-c',program]
    started=time.time();result=None
    try:
        with (directory/'cpu-check.stdout').open('w') as stdout,(directory/'cpu-check.stderr').open('w') as stderr:
            result=subprocess.run(argv,stdout=stdout,stderr=stderr,timeout=300)
    finally:
        subprocess.run(['docker','stop','--time','3',name],capture_output=True,timeout=10)
        copied=subprocess.run(['docker','cp',name+':/tmp/reedcode-cpu-check',str(output)],capture_output=True,timeout=20)
        removed=subprocess.run(['docker','rm',name],capture_output=True,timeout=10)
        summary={'image':args.image,'image_id':inspect['Id'],'docker_reported_image_size_bytes':inspect.get('Size'),
                 'expected_manifest_digest':expected_digest,'image_config_and_rootfs_verified':True,
                 'image_config_user':inspect['Config'].get('User'),'runtime_user_override':None if args.configured_root else '0',
                 'elapsed_seconds':time.time()-started,'exit_code':None if result is None else result.returncode,
                 'evidence_copy_exit_code':copied.returncode,'setup_helper_sha256':hashlib.sha256(helper.encode()).hexdigest(),
                 'cpu_only':True,'host_mounts':False,'container_removed':removed.returncode==0}
        (directory/'cpu-run.json').write_text(json.dumps(summary,indent=2)+'\n')
    if result is None or result.returncode or copied.returncode or removed.returncode:raise RuntimeError('CPU check failed; inspect retained evidence')
    print(json.dumps(summary))


if __name__=='__main__':main()
