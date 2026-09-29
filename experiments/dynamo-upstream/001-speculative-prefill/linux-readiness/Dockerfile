FROM rust:1.96.1-bookworm@sha256:a339861ae23e9abb272cea45dfafde21760d2ce6577a70f8a926153677902663

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv protobuf-compiler \
    clang libclang-dev lld cmake pkg-config libssl-dev libzmq3-dev \
    unzip zip && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/build-tools && \
    /opt/build-tools/bin/pip install --no-cache-dir 'maturin>=1,<2' patchelf

ENV PATH="/opt/build-tools/bin:/usr/local/cargo/bin:${PATH}" \
    CARGO_HOME=/work/cargo \
    CARGO_TARGET_DIR=/work/target \
    CARGO_BUILD_JOBS=2 \
    CARGO_INCREMENTAL=0 \
    CARGO_PROFILE_DEV_DEBUG=0 \
    CUDARC_CUDA_VERSION=13000 \
    SWAGGER_UI_DOWNLOAD_URL=file:///work/swagger-ui-v5.17.14.zip

WORKDIR /work
