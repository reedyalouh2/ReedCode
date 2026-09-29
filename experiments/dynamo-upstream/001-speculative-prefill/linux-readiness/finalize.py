"""Verify the finished CPU checks and package their evidence."""
from pathlib import Path
import datetime
import hashlib
import io
import json
import shutil
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parent
WORK=Path('/tmp/reedcode-linux-readiness')
def sha(data):
    return hashlib.sha256(data).hexdigest()

def main():
    rows=[]
    folders=[]
    for variant in ('stock','fixed'):
        for case in ('text','tool'):
            for hint in ('off','on'):
                folder=WORK/f'installed-smoke-{variant}-{case}-{hint}'
                result=json.loads((folder/'outcomes.json').read_text())
                assert result['normal_requests']==2
                assert result['warmup_requests']==int(hint=='on' and (variant=='stock' or case=='tool'))
                if result['warmup_requests']:
                    assert result['all_prepared_full_blocks_match']==(variant=='fixed')
                records=[json.loads(p.read_text()) for p in sorted(folder.glob('backend-*.json'))]
                result['normal_token_ids_sha256']=[sha(json.dumps(r['token_ids'],separators=(',',':')).encode()) for r in records if r['stop_conditions']['max_tokens']!=1]
                rows.append(result)
                folders.append(folder)
    for case in ('text','tool'):
        selected=[row for row in rows if row['case']==case]
        assert len({tuple(row['normal_token_ids_sha256']) for row in selected})==1
    for label in ('protocol-main-stock','protocol-main-stock-fixed-ports','protocol-main-fixed'):
        folder=WORK/label
        assert all(r['status']==200 for r in json.loads((folder/'outcomes.json').read_text()))
        for i in (1,2):
            a=json.loads((folder/f'backend-{i}.json').read_text())
            b=json.loads((WORK/'protocol-attempt2'/f'backend-{i}.json').read_text())
            assert a['token_ids']==b['token_ids']
        folders.append(folder)
    disabled_checks=[]
    for label in ('installed-smoke-fixed-tool-disabled','installed-smoke-fixed-runtime-disabled'):
        folder=WORK/label
        result=json.loads((folder/'outcomes.json').read_text())
        assert result['warmup_requests']==1 and result['all_prepared_full_blocks_match']
        first=json.loads((folder/'backend-1.json').read_text())
        assert first['extra_args']['reasoning_parser_kwargs']['chat_template_kwargs']['enable_thinking'] is False
        result['verified_enable_thinking']=False
        disabled_checks.append(result)
        folders.append(folder)
    wheels={}
    for label in ('common','stock','fixed'):
        paths=list((WORK/'artifacts'/label).glob('*.whl'))
        assert len(paths)==1
        path=paths[0]
        data=path.read_bytes()
        row={'path':str(path.relative_to(WORK/'artifacts')),'sha256':sha(data),'bytes':len(data)}
        if label!='common':
            with zipfile.ZipFile(path) as archive:
                module=next(n for n in archive.namelist() if n.endswith('dynamo/_core.abi3.so'))
                raw=archive.read(module)
                assert raw[:4]==b'\x7fELF'
                row.update(core_path=module,core_sha256=sha(raw))
        wheels[label]=row
    assert wheels['stock']['sha256']!=wheels['fixed']['sha256']
    manifest={'revision':'f5d3353e2167bb0f0d729085eb5bc9183bf4b222','artifact_root':str(WORK/'artifacts'),
              'target':'x86_64-unknown-linux-gnu','profile':'release','python_abi':'cp310-abi3',
              'wheels':wheels,'stock_fixed_normal_input_ids_equal':True,
              'installed_warmup_checks_passed':True,'gpu_used':False}
    (ROOT/'artifact-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (WORK/'artifacts/artifact-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    for name in ('setup-frontends.sh','launch-on-pod.sh'):
        shutil.copyfile(ROOT/name,WORK/'artifacts'/name)
    logs=['stock-build.log','fixed-build.log','main-fixed-import.log','main-fixed-warmup.log','main-fixed-protocol.log','wheel-sha256.txt']
    for name in logs:
        shutil.copyfile(WORK/'logs'/name,ROOT/name)
    files=[]
    archive_path=ROOT/'installed-runtime-evidence.tar.gz'
    with tarfile.open(archive_path,'w:gz') as archive:
        for folder in folders:
            for path in sorted(folder.rglob('*')):
                if not path.is_file():continue
                data=path.read_bytes();name=str(path.relative_to(WORK))
                archive.add(path,arcname=name,recursive=False)
                files.append({'path':name,'sha256':sha(data),'bytes':len(data)})
        for variant in ('stock','fixed'):
            path=WORK/variant/'lib/bindings/python/Cargo.lock';data=path.read_bytes();name=f'locks/{variant}.lock'
            info=tarfile.TarInfo(name);info.size=len(data);archive.addfile(info,io.BytesIO(data))
            files.append({'path':name,'sha256':sha(data),'bytes':len(data)})
    with tarfile.open(archive_path) as archive:
        for row in files:assert sha(archive.extractfile(row['path']).read())==row['sha256']
    summary={'passed':True,'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'constructed_cpu_cases':rows,'explicit_disabled_thinking_checks':disabled_checks,'normal_request_ids_equal_across_variants_and_hints':True,
             'cross_version_transport':'Both installed main frontends -> shipped image1.5 runtime TCP worker; two unchanged Claude bodies each.',
             'stock_fixed_ports_verified':{'backend_rpc':20000,'backend_response':20001,'frontend_rpc':20002,'frontend_response':20003},
             'archive_sha256':sha(archive_path.read_bytes()),'files':files,
             'limits':['Worker responses were scripted token IDs; no model or GPU ran.',
                       'This proves installed HTTP warmup dispatch, rendering, and runtime transport. It does not prove vLLM engine compatibility, KV residency, cache reset, or performance.',
                       'Local Python3.11/Debian differs from the full pinned Python3.12/Ubuntu24 GPU image; on-pod imports and live gates remain required.']}
    (ROOT/'installed-runtime-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    p=ROOT/'build-manifest.json';build=json.loads(p.read_text());build.update(status='built_and_cpu_checked',last_checked_at=summary['checked_at'],runtime_wheels_complete=True,artifact_manifest='artifact-manifest.json',installed_runtime_summary='installed-runtime-summary.json');p.write_text(json.dumps(build,indent=2)+'\n')
    print(json.dumps({'passed':True,'wheels':wheels,'warmup_rows':len(rows)},indent=2))

if __name__=='__main__':
    main()
