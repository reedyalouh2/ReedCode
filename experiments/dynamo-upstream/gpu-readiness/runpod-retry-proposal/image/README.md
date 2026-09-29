# USER 0 image metadata

`manifest.json` identifies the deployed derivative. Its digest is `sha256:0a0741342e709bbfa6689e688c8306090af4a65d0f366ec38ec0269d63c201c6`.

`config.json` changes only `config.User` from `dynamo` to `0`. All 67 layer descriptors, rootfs diff IDs, history and other settings match the pinned parent. `prepare_image.py` fetches and verifies the parent manifest, verifies the saved config, and constructs these files. `proof.json` records the checks.

`../publication.json` records the published image. All parent layer sizes were checked, and the published config and manifest matched the saved bytes. A Docker rebuild may add a history entry and produce a different digest.

The layers total 9,624,744,034 compressed bytes. A guarded local pull completed in 317.67 seconds. Docker reports 32,491,416,799 bytes for the image; this is Docker's reported size, not a separately measured unpacked-filesystem total. Free disk stayed above the 8 GiB floor, with a minimum of 11,596,939,264 bytes.

The parent image passed a CPU check with Docker's `--user 0` override. `cpu-validation/` contains that evidence.

The published derivative then passed the same check using its configured user, with no override. Docker's config and all 67 rootfs diff IDs matched the saved metadata. Effective UID was 0 and `CAP_NET_RAW` was present. A normal apt install supplied tcpdump; the test recovered its fixed payload from eight loopback packets with zero drops. The base Python package map stayed identical. The check took 17.16 seconds, used no GPU or host mounts, and removed its container afterward. `derived-cpu-validation/` contains the raw evidence and hashes. The replacement pod still needs its own capability and live serving checks.
