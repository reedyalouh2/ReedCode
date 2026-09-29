"""Check the patched native renderer against the recorded and length-control inputs."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib

ROOT=Path(__file__).resolve().parent

def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(binary, output, tokenizer):
    source=ROOT.parent/'session-cost'
    previous=json.loads((source/'results.json').read_text())
    for name, evidence in previous['raw_evidence'].items():
        if digest((source/name).read_bytes()) != evidence['sha256']:
            raise ValueError('Source evidence changed: '+name)
    original_inputs=gzip.decompress((source/'input.json.gz').read_bytes())
    input_data=json.loads(original_inputs)
    if digest(tokenizer.read_bytes()) != previous['tokenizer_sha256']:
        raise ValueError('Tokenizer differs from the verified source study')
    input_data['config_path']=str((ROOT.parents[2]/'dynamo-prefix/fixtures/tokenizer_config.json').resolve())
    input_data['tokenizer_path']=str(tokenizer.resolve())
    inputs=json.dumps(input_data,indent=2).encode()
    stock=json.loads(gzip.decompress((source/'native-output.json.gz').read_bytes()))
    with tempfile.NamedTemporaryFile(suffix='.json') as f:
        f.write(inputs); f.flush()
        raw=subprocess.check_output([str(binary),f.name])
    rows=json.loads(raw)
    if len(rows)!=len(stock):
        raise ValueError('Missing native cases')
    summary=[]
    for fixed, old in zip(rows,stock):
        if fixed['case']!=old['case'] or fixed['original_ids']!=old['original']['token_ids'] or fixed['followup_ids']!=old['followup']['token_ids']:
            raise ValueError('Normal request tokens changed: '+fixed['case'])
        prepared=fixed['prepared_ids']
        if prepared is None or fixed['followup_ids'][:len(prepared)]!=prepared:
            raise ValueError('Fixed preparation is not an exact prefix: '+fixed['case'])
        summary.append({'case':fixed['case'],'prepared_tokens':len(prepared),'complete_blocks':len(prepared)//16,
                        'exact_prefix':True,'warmup_only_full_blocks_after_followup':0})
    native=gzip.compress(raw,mtime=0)
    output.mkdir(parents=True,exist_ok=True)
    (output/'native-output.json.gz').write_bytes(native)
    (output/'input.json.gz').write_bytes(gzip.compress(inputs,mtime=0))
    pilot_root=ROOT.parent/'current-code'
    pilot_source=(pilot_root/'input.json').read_bytes()
    pilot_stock=(pilot_root/'compiled/main/output.json').read_bytes()
    pilot_report=json.loads((pilot_root/'results.json').read_text())
    if digest(pilot_source)!=pilot_report['input_sha256'] or digest(pilot_stock)!=next(r['output_sha256'] for r in pilot_report['runs'] if r['label']=='main'):
        raise ValueError('Pilot evidence changed')
    pilot_input=json.loads(pilot_source)
    pilot_input.update({key:input_data[key] for key in ('config_path','tokenizer_path')})
    with tempfile.NamedTemporaryFile(suffix='.json') as f:
        f.write(json.dumps(pilot_input).encode()); f.flush()
        pilot_raw=subprocess.check_output([str(binary),f.name])
    pilot=[]
    for fixed,old in zip(json.loads(pilot_raw),json.loads(pilot_stock),strict=True):
        if fixed['case']!=old['case'] or fixed['original_ids']!=old['original']['token_ids'] or fixed['followup_ids']!=old['followup']['token_ids']:
            raise ValueError('Normal pilot request changed')
        prepared=fixed['prepared_ids']
        if fixed['case']=='tool':
            if not prepared or fixed['followup_ids'][:len(prepared)]!=prepared:
                raise ValueError('Pilot tool prefix still differs')
        elif prepared is not None:
            raise ValueError('Unsupported text pilot must skip')
        pilot.append({'case':fixed['case'],'prepared_tokens':None if prepared is None else len(prepared),
                      'outcome':'skipped' if prepared is None else 'exact_prefix','normal_requests_unchanged':True})
    (output/'pilot-native-output.json').write_bytes(pilot_raw)
    upstream=tomllib.loads((ROOT.parent/'current-code/sources/main/Cargo.lock').read_text())
    lock=tomllib.loads((ROOT/'Cargo.lock').read_text())
    identity=lambda p:(p['name'],p['version'],p.get('source'),p.get('checksum'))
    known={identity(p) for p in upstream['package']}
    registry=[p for p in lock['package'] if p.get('source','').startswith('registry+')]
    changed=[identity(p) for p in registry if identity(p) not in known]
    if changed: raise ValueError('Dependency drift: '+str(changed))
    report={'source_revision':'f5d3353e2167bb0f0d729085eb5bc9183bf4b222','renderer':'5.4.0 with local patch',
            'recorded_tool_continuations':42,'constructed_length_cases':12,'exact_prefixes':len(rows),
            'normal_requests_unchanged':True,'gpu_measurements':False,'future_tool_content_used_to_prepare':False,
            'completed_assistant_source':'captured followup message at original message count; tool-result messages are withheld from preparation',
            'source_input_sha256':digest(original_inputs),'input_sha256':digest(inputs),'native_output_sha256':digest(native),'native_output_uncompressed_sha256':digest(raw),
            'binary_sha256':digest(binary.read_bytes()),'registry_dependencies':len(registry),'dependency_drift':changed,
            'isolated_pilot':{'cases':pilot,'input_sha256':digest(pilot_source),'stock_sha256':digest(pilot_stock),'native_output_sha256':digest(pilot_raw)},
            'files':{name:digest((ROOT/name).read_bytes()) for name in ['build.py','reproduce.py','check.rs','dynamo.patch','renderer.patch','dynamo/accumulator.rs','dynamo/contract.rs','renderer/speculative.rs','renderer/qwen3-tool-prefix.jinja','Cargo.lock','cpu-tests.log']},
            'cases':summary,
            'limits':['This report covers native CPU token parity; full module checks are recorded in integration-results.json.',
                      'Exact Qwen template and tokenizer contract only; text continuations and unsupported settings skip.',
                      'Client must preserve completed messages and effective rendering fields when appending tool results.',
                      'Zero warmup-only blocks after a matching followup is input-block accounting, not physical GPU residency.']}
    (output/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'{len(rows)} exact fixed prefixes; normal tokens unchanged; {len(registry)} registry dependency identities match upstream.')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binary',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--tokenizer',type=Path,default=Path('/tmp/reedcode-prefix-tokenizer/tokenizer.json'))
    a=p.parse_args();run(a.binary,a.output,a.tokenizer)
