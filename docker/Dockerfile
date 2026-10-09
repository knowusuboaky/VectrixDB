# syntax=docker/dockerfile:1
#
# VectrixDB in a container: two images from one file.
#
#   server   (the default) the REST API and the dashboard:
#            vectrixdb serve, on port 7337
#   extract  the extraction service, files, addresses and videos in and text
#            out: vectrixdb extract-serve, on port 7338
#
# From the repository root:
#
#   docker build -f docker/Dockerfile -t vectrixdb .
#   docker build -f docker/Dockerfile --target extract -t vectrixdb-extract .
#   docker build -f docker/Dockerfile --build-arg MODELS=all -t vectrixdb:full .
#
# What both images hold to, on purpose:
#
#   - Nothing runs as root. The user is 10001 in group 0, so a platform that
#     picks its own user id, OpenShift for one, can still write /data.
#   - The root filesystem can be mounted read-only. Everything written goes
#     to /data, the one volume, or to /tmp.
#   - No system package is added. The image is the Python base image, pinned
#     by its digest and brought up to date with Debian's security fixes as it
#     is built, and Python packages from PyPI, so every layer is accounted for
#     in the SBOM a release attaches.
#   - The models it uses are inside it. Nothing is downloaded at run time,
#     which is what an air-gapped or egress-controlled network needs.
#   - A small init runs first, so a stop signal reaches the server, requests in
#     flight finish, and processes a reader started are reaped.
#
# Behind a company proxy that inspects TLS, hand the build its certificate for
# the length of the build only; it is never written into a layer:
#
#   docker build --secret id=ca,src=company-ca.pem -f docker/Dockerfile .
#
# A company that mirrors base images names its copy with
# --build-arg PYTHON_IMAGE=registry.example.com/python:3.12-slim, and one whose
# builds cannot reach Debian's archive adds --build-arg DEBIAN_UPDATES=0.
#
# A release builds from the very wheel it uploaded to PyPI rather than from
# the source here: docker build --build-context wheels=dist ...

ARG PYTHON_IMAGE=python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016


# ============================================================================
# THE WHEEL
# ============================================================================
#
# INPUT   the package source and the files its metadata reads
# OUTPUT  vectrixdb-<version>-py3-none-any.whl, alone in the stage "wheels"
#
# Built the way PyPI's is. A stage called "wheels" is what the images install
# from, so --build-context wheels=dist swaps in a wheel built elsewhere.

FROM ${PYTHON_IMAGE} AS wheel-build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY vectrixdb ./vectrixdb
RUN --mount=type=secret,id=ca \
    if [ -s /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    PIP_DISABLE_PIP_VERSION_CHECK=1 pip wheel --no-cache-dir --no-deps --wheel-dir /out .

FROM scratch AS wheels
COPY --from=wheel-build /out/ /


# ============================================================================
# THE ENVIRONMENTS: one per image
# ============================================================================
#
# INPUT   the wheel, and the extras each image serves
# OUTPUT  /opt/vectrixdb, a virtual environment the image copies whole
#
# Installed in a stage of its own and copied across, so no build cache, no
# pip cache and no wheel is left in the image.

FROM ${PYTHON_IMAGE} AS venv
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PATH=/opt/vectrixdb/bin:$PATH
RUN python -m venv /opt/vectrixdb
COPY --from=wheels / /wheels/

# The server: every storage backend a company is likely to point it at, sign-in,
# MCP, tracing, the readers for PDF and Office files, and the feed reader for
# the sources a collection keeps up with. Scans, recordings and videos are the
# extraction image's job, or Azure's.
FROM venv AS server-venv
ARG SERVER_EXTRAS=api,signin,mcp,tracing,documents,feeds,azure,aws,postgres,jobs-azure
# all: the multilingual embedding model and reranker as well, about 280 MB
# more, for collections in the hundred-odd languages besides English.
ARG MODELS=
# The package is the very wheel being released. What it pulls in is what pip
# resolves on the day of the build, and the SBOM a release attaches records it.
# hadolint ignore=DL3013
RUN --mount=type=secret,id=ca \
    if [ -s /run/secrets/ca ]; then \
        export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca REQUESTS_CA_BUNDLE=/run/secrets/ca; \
    fi; \
    set -eu; \
    wheel="$(ls /wheels/vectrixdb-*.whl)"; \
    pip install "dumb-init==1.2.5" "${wheel}[${SERVER_EXTRAS}]"; \
    if [ "${MODELS}" = "all" ]; then \
        vectrixdb download-models --type all; \
    elif [ -n "${MODELS}" ]; then \
        echo "MODELS is all, or empty for the models the package carries" >&2; exit 2; \
    fi

# The extraction service: every reader on the machine (PDF, Office, OCR,
# speech, video, YouTube) and the Azure services that can stand in for them.
FROM venv AS extract-venv
ARG EXTRACT_EXTRAS=api,signin,tracing,extract,youtube,ocr-azure,jobs-azure
# The faster-whisper model recordings are read with when no speech service is
# set, fetched now so the service reads them with no network. Empty leaves it
# out, for a deployment that always has Azure Speech.
ARG WHISPER_MODEL=base
ENV HF_HOME=/opt/vectrixdb/huggingface
# hadolint ignore=DL3013
RUN --mount=type=secret,id=ca \
    if [ -s /run/secrets/ca ]; then \
        export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca REQUESTS_CA_BUNDLE=/run/secrets/ca; \
    fi; \
    set -eu; \
    wheel="$(ls /wheels/vectrixdb-*.whl)"; \
    pip install "dumb-init==1.2.5" "${wheel}[${EXTRACT_EXTRAS}]"; \
    # RapidOCR asks for OpenCV's desktop build, which needs X11 and OpenGL
    # libraries a server image has no use for. The headless build is the same
    # library without the windows, at the same version.
    version="$(python -c 'import importlib.metadata as m; print(m.version("opencv-python"))' 2>/dev/null || true)"; \
    if [ -n "${version}" ]; then \
        pip uninstall -y opencv-python; \
        pip install "opencv-python-headless==${version}"; \
    fi; \
    python -c "import cv2, rapidocr_onnxruntime, faster_whisper, imageio_ffmpeg"; \
    # The embedding and reranking models the package carries are the server's.
    # This service reads and never embeds, so they are about 100 MB of nothing.
    for model in bge_small_en colbert reranker_en; do \
        rm -rf /opt/vectrixdb/lib/python3*/site-packages/vectrixdb/models/data/"${model}"; \
    done; \
    if [ -n "${WHISPER_MODEL}" ]; then \
        python -c "import sys; from faster_whisper.utils import download_model; download_model(sys.argv[1])" "${WHISPER_MODEL}"; \
    fi


# ============================================================================
# THE RUNTIME: what both images share
# ============================================================================
#
# INPUT   the base image
# OUTPUT  a user that is not root, /data, and the environment both servers read
#
# Debian's security fixes published since the base image was built are
# applied first, so a scan of a fresh build finds nothing that has a fix.
# HOME is /data so a library that writes beside the home directory writes to
# the volume and not to a read-only root; caches go to /tmp. Two settings
# differ from the library's own defaults, because an image listens on every
# address: a read needs a key like everything else (VECTRIXDB_OPEN_READS=0;
# set it to 1 for the library's open reads), and the access log goes to the
# output the platform collects.

FROM ${PYTHON_IMAGE} AS runtime
# 0 keeps the base image exactly as pinned, for a build Debian is out of reach of.
ARG DEBIAN_UPDATES=1
ENV PATH=/opt/vectrixdb/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    XDG_CACHE_HOME=/tmp/cache \
    VECTRIXDB_PATH=/data \
    VECTRIXDB_OPEN_READS=0 \
    VECTRIXDB_ACCESS_LOG=stdout
RUN set -eu; \
    if [ "${DEBIAN_UPDATES}" = "1" ]; then \
        apt-get update || { \
            echo "Debian's archive is out of reach: build with --build-arg DEBIAN_UPDATES=0" >&2; \
            exit 1; \
        }; \
        DEBIAN_FRONTEND=noninteractive apt-get -y upgrade; \
        apt-get clean; \
        rm -rf /var/lib/apt/lists/*; \
    elif [ "${DEBIAN_UPDATES}" != "0" ]; then \
        echo "DEBIAN_UPDATES is 1 or 0" >&2; exit 2; \
    fi; \
    useradd --uid 10001 --gid 0 --home-dir /data --no-create-home \
        --shell /usr/sbin/nologin vectrixdb; \
    mkdir -p /data; \
    chown 10001:0 /data; \
    chmod 0770 /data
WORKDIR /data
VOLUME ["/data"]
ARG VERSION=dev
ARG REVISION=
ARG CREATED=
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.source="https://github.com/knowusuboaky/VectrixDB" \
      org.opencontainers.image.url="https://knowusuboaky.github.io/VectrixDB/how-to/containers/" \
      org.opencontainers.image.documentation="https://knowusuboaky.github.io/VectrixDB/how-to/containers/" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="VectrixDB"


# ============================================================================
# THE EXTRACTION IMAGE
# ============================================================================
#
# INPUT   the runtime and the extraction environment
# OUTPUT  vectrixdb extract-serve on port 7338, with a health check
#
# It will not start without VECTRIXDB_API_KEY or sign-in: every call can spend
# money on a paid service. Long jobs write under /data/output.

FROM runtime AS extract
COPY --from=extract-venv /opt/vectrixdb /opt/vectrixdb
ENV VECTRIXDB_EXTRACT_LISTEN_PORT=7338 \
    VECTRIXDB_EXTRACT_OUTPUT=/data/output \
    HF_HOME=/opt/vectrixdb/huggingface \
    HF_HUB_OFFLINE=1
LABEL org.opencontainers.image.title="VectrixDB extraction service" \
      org.opencontainers.image.description="Files, addresses and videos in, text out: PDF, Office, OCR, speech, video and YouTube readers, on the machine or through Azure."
USER 10001:0
EXPOSE 7338
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; p = os.environ.get('VECTRIXDB_EXTRACT_PREFIX', '').strip().strip('/'); u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%s%s/health' % (os.environ.get('VECTRIXDB_EXTRACT_LISTEN_PORT', '7338'), '/' + p if p else ''), timeout=4)"]
ENTRYPOINT ["dumb-init", "--", "vectrixdb"]
CMD ["extract-serve", "--host", "0.0.0.0"]


# ============================================================================
# THE SERVER IMAGE (the default target)
# ============================================================================
#
# INPUT   the runtime and the server environment
# OUTPUT  vectrixdb serve on port 7337, with a health check
#
# It will not listen without VECTRIXDB_API_KEY (or its _FILE twin), sign-in,
# or VECTRIXDB_ALLOW_OPEN=1 for a gateway that does the asking.

FROM runtime AS server
COPY --from=server-venv /opt/vectrixdb /opt/vectrixdb
ENV VECTRIXDB_LISTEN_PORT=7337
LABEL org.opencontainers.image.title="VectrixDB" \
      org.opencontainers.image.description="The VectrixDB server: REST API and dashboard, with the embedding and reranking models inside."
USER 10001:0
EXPOSE 7337
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%s/health' % os.environ.get('VECTRIXDB_LISTEN_PORT', '7337'), timeout=4)"]
ENTRYPOINT ["dumb-init", "--", "vectrixdb"]
CMD ["serve", "--host", "0.0.0.0"]
