# Runpod readiness check

The installed and current released CLI is **runpodctl 2.14.0**. Its `pod create` help has no `--terminate-after` or `--stop-after` flag. The public API schema has no pod scheduling field.

[PR #330](https://github.com/runpod/runpodctl/pull/330) removed those flags because the backend accepted their values without stopping or deleting the pod. [PR #331](https://github.com/runpod/runpodctl/pull/331), which would restore the flags, is still open and marked draft. Its description says the backend must enforce the timer before it can merge. These were read on September 28, 2026 (September 29 UTC).

The [manifest](manifest.json) hashes the public API responses, API schema, local version and help output. These fetches used no credentials. The original combined plan's provider-timer requirement cannot currently be met through these interfaces.

The replacement key was saved outside the repo in a user-only local file and validated with read-only account calls. The account reported no pods and zero active spend. Both A100 80GB variants were quoted at $1.59/hour in secure cloud; the quote must be checked again immediately before creation. The user confirmed the earlier key was revoked. No pod has been created for this study.

## Approved local substitute

Run [watch_pod.py](../watch_pod.py) in a detached, sleep-inhibited local process alongside the study controller. Record allocation start before making the create call. The guard targets only the newly recorded pod ID, verifies its name and quote, closes admission at 130 minutes and begins deletion at 140 minutes. If readiness has not completed, it begins deletion at 35 minutes. It retries failed deletion and requires the pod to be absent from a fresh account listing before recording cleanup.

Run the checks with:

```sh
python3 -m unittest discover -s experiments/dynamo-upstream/gpu-readiness -p test_watch_pod.py -v
```

Eleven CPU tests pass, including API failures, delayed deletion, a local clock rollback, stale watchdog detection, allocation reconciliation by exact study name, and controller cleanup after a study error. No actual deletion has been exercised. The guard refuses to start without approval of the changed control recorded in its state file.

[study_cleanup.py](../study_cleanup.py) deletes in the controller's exit path and can reconcile an uncertain create response by its unique study name. It requires a fresh heartbeat from the detached guard before starting the study. Never retry an uncertain create call: first reconcile the existing allocation. Wiring these helpers into the frozen launch command and testing process detachment remain deployment gates.

A local guard cannot act while the Mac is off or cannot reach Runpod. This is a weaker cutoff than the provider timer in the original proposal. The user approved this substitute after that limitation was explained; the $5 ceiling stays in place. The other pre-rental gates still apply.
