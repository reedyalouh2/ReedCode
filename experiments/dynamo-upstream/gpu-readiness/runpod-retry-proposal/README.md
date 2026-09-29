# Proposed Runpod retry

Status: approved by the user on September 28, 2026. The metadata change is prepared and the parent image is being copied to the registry. No replacement GPU pod has been requested.

The first pod ran the pinned image as UID 1000 with no effective capabilities and no tcpdump. It was deleted before model download. Runpod's checked API and CLI provide no supported user override.

The image changes only the container's runtime user to root. It inherits every filesystem layer from the recorded Dynamo 1.5.0 image. Dynamo, vLLM, CUDA, the model revision and the study settings stay the same. The parity report must name both the parent digest and the new digest. The user approved this change to the original exact-image requirement.

The [image proof](image/proof.json) records the config diff and matching filesystem layers. The image contains no code from the patched frontend and no credentials. A CPU check of the exact parent with `--user 0` passed: normal root package installation and loopback capture worked, with unchanged Python packages. [Evidence](image/cpu-validation/cpu-run.json). The published derivative still needs its own CPU check before the replacement rental.

The proposed rental is one replacement A100, at no more than $1.65/hour. Keep the combined spending cap at $5. Reserve $0.17 for the first attempt until its bill settles; the replacement has at most $4.83 available. Use the same local watchdog and independent cleanup controller, the 35-minute readiness gate and the minute-140 deletion deadline. Do the live UID/capture check before model download.

Once capture passes, run the approved stock-vs-fix comparison and both real-client parity sessions. Keep the same model, thinking settings, request limits, raw evidence and analysis.

The approval covers the derived image, its publication for deployment, and one replacement GPU rental within the existing total cap. A separate proposal for a CPU-only image-transfer helper is pending; it has not been created. Nothing will be filed or pushed upstream as part of this retry.
