"""Save the runtime framing source at the deployed image revision."""

import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
REVISION = "32b8b2f8c63fa3531c34b64c1cf2cbe39a6f9653"
PATHS = ["lib/runtime/src/pipeline/network.rs", "lib/runtime/src/pipeline/network/codec.rs",
         "lib/runtime/src/pipeline/network/codec/two_part.rs", "lib/runtime/src/pipeline/network/tcp.rs",
         "lib/runtime/src/pipeline/network/ingress/push_handler.rs"]


def main():
    output = io.BytesIO()
    records = []
    with tarfile.open(fileobj=output, mode="w") as archive:
        for path in PATHS:
            url = f"https://raw.githubusercontent.com/ai-dynamo/dynamo/{REVISION}/{path}"
            with urlopen(url, timeout=30) as response:
                raw = response.read()
            item = tarfile.TarInfo(path)
            item.size, item.mode, item.mtime = len(raw), 0o644, 0
            archive.addfile(item, io.BytesIO(raw))
            records.append({"path": path, "url": url, "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()})
    packed = gzip.compress(output.getvalue(), mtime=0)
    (ROOT / "source.tar.gz").write_bytes(packed)
    (ROOT / "source.json").write_text(json.dumps({"revision": REVISION, "files": records,
         "archive_sha256": hashlib.sha256(packed).hexdigest()}, indent=2) + "\n")
    print(f"Saved {len(records)} pinned runtime files")


if __name__ == "__main__":
    main()
