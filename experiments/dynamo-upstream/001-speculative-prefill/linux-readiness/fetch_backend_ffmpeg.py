"""Read the public FFmpeg layer from the pinned backend image."""

import hashlib
import json
from pathlib import Path
import re
import tarfile
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
WORK = Path("/tmp/reedcode-linux-readiness")
REPOSITORY = "nvidia/ai-dynamo/vllm-runtime"
DIGEST = "sha256:d18389c89eb319401fdd73f1fbbaff10d9382f634b879941dfba75bbf7260c1e"
ACCEPT = "application/vnd.oci.image.manifest.v1+json"


def main():
    url = f"https://nvcr.io/v2/{REPOSITORY}/manifests/{DIGEST}"
    headers = {"Accept": ACCEPT}
    try:
        response = urlopen(Request(url, headers=headers), timeout=60)
    except HTTPError as error:
        if error.code != 401:
            raise
        challenge = dict(re.findall(r'(\w+)="([^"]+)"', error.headers["WWW-Authenticate"]))
        realm = challenge.pop("realm")
        if not realm.startswith("https://nvcr.io/"):
            raise ValueError("Unexpected public registry auth endpoint")
        # This anonymous pull token is used in memory and is never written to the report.
        with urlopen(realm + "?" + urlencode(challenge), timeout=60) as auth:
            token = json.load(auth)["token"]
        headers["Authorization"] = "Bearer " + token
        response = urlopen(Request(url, headers=headers), timeout=60)
    with response:
        raw_manifest = response.read()
    if hashlib.sha256(raw_manifest).hexdigest() != DIGEST.split(":")[1]:
        raise ValueError("Registry manifest digest mismatch")
    manifest = json.loads(raw_manifest)
    layer = manifest["layers"][50]
    expected_layer = json.loads((ROOT / "backend-image-manifest.json").read_text())["layers"][50]
    if layer != expected_layer:
        raise ValueError("Wheelhouse layer differs from inspected manifest")
    destination = WORK / "backend-image-ffmpeg-layer.tar.gz"
    hasher = hashlib.sha256()
    with urlopen(Request(f"https://nvcr.io/v2/{REPOSITORY}/blobs/{layer['digest']}", headers=headers), timeout=120) as stream:
        with destination.open("xb") as output:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
                output.write(chunk)
    if hasher.hexdigest() != layer["digest"].split(":")[1] or destination.stat().st_size != layer["size"]:
        raise ValueError("Registry layer digest or size mismatch")
    destination_root = WORK / "backend-ffmpeg"
    destination_root.mkdir(exist_ok=True)
    files = []
    with tarfile.open(destination) as archive:
        for member in archive:
            name = Path(member.name)
            if not member.isfile() or not name.as_posix().startswith("usr/local/"):
                continue
            if ".." in name.parts:
                raise ValueError("Unsafe layer path")
            if not (name.as_posix().startswith("usr/local/lib/") or name.as_posix().startswith("usr/local/include/")):
                continue
            raw = archive.extractfile(member).read()
            target = destination_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            files.append({"name": str(name), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    if not files:
        raise ValueError("Pinned FFmpeg layer contained no libraries")
    (ROOT / "backend-ffmpeg-evidence.json").write_text(json.dumps({
        "image": f"nvcr.io/{REPOSITORY}@{DIGEST}", "manifest_digest_verified": True,
        "layer": layer, "layer_digest_verified": True, "layer_index_zero_based": 50,
        "files": files, "extraction_only": True,
        "limitation": "Actual shipped FFmpeg libraries and headers; source tree and binary omitted."
    }, indent=2) + "\n")
    print(json.dumps(files, indent=2))


if __name__ == "__main__":
    main()
