"""Publish the verified runtime-user config after all parent blobs are present."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlencode
from urllib.request import Request, urlopen


def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def publish(root, execute):
    publication_path = root/'publication.json'
    publication = json.loads(publication_path.read_text())
    proof = json.loads((root/'image/proof.json').read_text())
    manifest = (root/'image/manifest.json').read_bytes()
    config = (root/'image/config.json').read_bytes()
    parent = json.loads((root/'image/parent-manifest.json').read_text())
    parsed = json.loads(manifest)
    assert sha(manifest) == proof['derived_manifest_digest'] == publication['derived_digest']
    assert sha(config) == proof['derived_config_digest'] == parsed['config']['digest']
    assert parsed['layers'] == parent['layers']
    assert json.loads(config)['config']['User'] == '0'
    repository = publication['repository']
    if not repository.startswith('ttl.sh/reedcode-dynamo-') or '/' in repository[len('ttl.sh/'):]:
        raise ValueError('Expected the task-specific registry repository')
    base = 'https://ttl.sh/v2/' + repository.split('/',1)[1]
    if not execute:
        print(json.dumps({'execute':False,'image':repository+'@'+sha(manifest)})); return
    for layer in parsed['layers']:
        with urlopen(Request(base+'/blobs/'+layer['digest'],method='HEAD'),timeout=60) as r:
            if int(r.headers['Content-Length']) != layer['size']:
                raise ValueError('Published parent blob size mismatch')
    with urlopen(Request(base+'/blobs/uploads/',data=b'',method='POST'),timeout=60) as r:
        location = urljoin(base+'/',r.headers['Location'])
    if urlsplit(location).scheme != 'https' or urlsplit(location).netloc != 'ttl.sh':
        raise ValueError('Unexpected upload destination')
    separator = '&' if urlsplit(location).query else '?'
    with urlopen(Request(location+separator+urlencode({'digest':sha(config)}),data=config,method='PUT',
                         headers={'Content-Type':'application/octet-stream'}),timeout=60) as r:
        if r.status != 201: raise ValueError('Config upload did not complete')
    with urlopen(Request(base+'/manifests/24h',data=manifest,method='PUT',
                         headers={'Content-Type':parsed['mediaType']}),timeout=60) as r:
        if r.status != 201 or r.headers.get('Docker-Content-Digest') != sha(manifest):
            raise ValueError('Manifest publication digest mismatch')
    with urlopen(Request(base+'/manifests/'+sha(manifest),headers={'Accept':parsed['mediaType']}),timeout=60) as r:
        remote = r.read()
    if remote != manifest: raise ValueError('Read-back manifest differs')
    with urlopen(base+'/blobs/'+sha(config),timeout=60) as r:
        if r.read() != config: raise ValueError('Read-back config differs')
    publication.update(status='published_and_verified',image=repository+'@'+sha(manifest),
                       parent_layers_verified=len(parsed['layers']),config_user='0')
    publication_path.write_text(json.dumps(publication,indent=2)+'\n')
    print(json.dumps(publication,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    publish(Path(__file__).resolve().parent,p.parse_args().execute)
