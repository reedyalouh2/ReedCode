"""Prepare a USER 0 image config and manifest without copying filesystem layers."""
import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parents[1]/'001-speculative-prefill/linux-readiness'
REPOSITORY = 'nvidia/ai-dynamo/vllm-runtime'
PARENT = 'sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e'
ACCEPT = 'application/vnd.oci.image.manifest.v1+json'


def digest(data):
    return 'sha256:'+hashlib.sha256(data).hexdigest()


def main():
    output=ROOT/'image'
    output.mkdir(exist_ok=False)
    headers={'Accept':ACCEPT}
    url=f'https://nvcr.io/v2/{REPOSITORY}/manifests/{PARENT}'
    try:
        response=urlopen(Request(url,headers=headers),timeout=30)
    except HTTPError as error:
        if error.code!=401:raise
        challenge=dict(re.findall(r'(\w+)="([^"]+)"',error.headers['WWW-Authenticate']))
        realm=challenge.pop('realm')
        if not realm.startswith('https://nvcr.io/'):raise ValueError('Unexpected anonymous token endpoint')
        with urlopen(realm+'?'+urlencode(challenge),timeout=30) as auth:
            token=json.load(auth)['token']
        headers['Authorization']='Bearer '+token
        response=urlopen(Request(url,headers=headers),timeout=30)
    with response:parent_raw=response.read()
    if digest(parent_raw)!=PARENT:raise ValueError('Parent manifest digest mismatch')
    parent=json.loads(parent_raw)
    if parent!=json.loads((SOURCE/'backend-image-manifest.json').read_text()):
        raise ValueError('Parent differs from previously inspected immutable image')
    config_raw=(SOURCE/'backend-image-config.json').read_bytes()
    if digest(config_raw)!=parent['config']['digest'] or len(config_raw)!=parent['config']['size']:
        raise ValueError('Saved config differs from the parent descriptor')
    original=json.loads(config_raw)
    if original['config']['User']!='dynamo':raise ValueError('Unexpected original user')
    needle=b'"User":"dynamo"'
    if config_raw.count(needle)!=1:raise ValueError('Expected one canonical user setting')
    derived_raw=config_raw.replace(needle,b'"User":"0"',1)
    derived=json.loads(derived_raw)
    expected=copy.deepcopy(original);expected['config']['User']='0'
    if derived!=expected:raise ValueError('Unexpected config change')
    manifest=copy.deepcopy(parent)
    manifest['config']['digest']=digest(derived_raw)
    manifest['config']['size']=len(derived_raw)
    manifest_raw=json.dumps(manifest,separators=(',',':')).encode()
    assert manifest['layers']==parent['layers']
    assert derived['rootfs']==original['rootfs']
    assert derived['history']==original['history']
    for name,data in (('parent-manifest.json',parent_raw),('parent-config.json',config_raw),
                      ('config.json',derived_raw),('manifest.json',manifest_raw)):
        (output/name).write_bytes(data)
    proof={'parent_repository':'nvcr.io/'+REPOSITORY,'parent_manifest_digest':PARENT,
           'parent_config_digest':digest(config_raw),'derived_manifest_digest':digest(manifest_raw),
           'derived_config_digest':digest(derived_raw),'changed_config_fields':{'config.User':{'before':'dynamo','after':'0'}},
           'filesystem_layers_identical':True,'rootfs_diff_ids_identical':True,'history_identical':True,
           'layer_count':len(parent['layers']),'compressed_layer_bytes':sum(row['size'] for row in parent['layers']),
           'new_config_bytes':len(derived_raw),'new_manifest_bytes':len(manifest_raw),
           'full_image_layers_downloaded':False,'cpu_runtime_test_performed':False,
           'uncompressed_layer_bytes':None,
           'uncompressed_size_note':'The manifest has compressed sizes and diff IDs only; exact unpacked size requires layer data.',
           'registry_uploaded':False,
           'handoff':'Upload config, copy or mount every original layer blob into the destination repository, then put this exact manifest. Verify the destination digest and layer list before use.'}
    (output/'proof.json').write_text(json.dumps(proof,indent=2)+'\n')
    print(json.dumps(proof,indent=2))


if __name__=='__main__':main()
