# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- build wheels
# The GSSAPI and krb5 bindings are C extensions built against MIT krb5, and
# PyPI has no Linux wheels for them, so they are compiled here. That is the
# slow part of the build, so the dependencies get a layer of their own that
# depends on pyproject.toml alone: a code change reuses it from the cache.
FROM python:${PYTHON_VERSION}-slim AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev libkrb5-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['kerberos']))" > /tmp/requirements.txt \
 && pip wheel --wheel-dir /wheels -r /tmp/requirements.txt

COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --no-deps --wheel-dir /wheels .

# ---------------------------------------------------------------- tests
# Used by tests/integration/docker-compose.yaml; never shipped.
FROM python:${PYTHON_VERSION}-slim AS test
RUN apt-get update \
 && apt-get install -y --no-install-recommends libkrb5-3 libgssapi-krb5-2 krb5-user \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl pytest pytest-timeout
WORKDIR /src
COPY tests ./tests
COPY pyproject.toml ./
ENTRYPOINT ["pytest", "-m", "integration", "--timeout", "600", "tests/integration"]

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim

ARG VERSION=dev

LABEL org.opencontainers.image.title="dapr-smb-binding" \
      org.opencontainers.image.description="SMB input and output binding for Dapr (Kerberos, user space), as a pluggable component" \
      org.opencontainers.image.source="https://github.com/integrio-intropy/dapr-smb-binding" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

# MIT krb5 runtime libraries only: no compiler, no krb5-user tools.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libkrb5-3 libgssapi-krb5-2 \
 && rm -rf /var/lib/apt/lists/* \
 # 65532 is the uid and gid daprd runs as. Sharing them lets the sidecar use
 # the 0660 socket without relying on the pod's fsGroup.
 && groupadd --system --gid 65532 app \
 && useradd --system --uid 65532 --gid app --no-create-home app \
 && mkdir -p /tmp/dapr-components-sockets /tmp/dapr-smb-binding \
 && chown app:app /tmp/dapr-components-sockets /tmp/dapr-smb-binding \
 && chmod 0700 /tmp/dapr-smb-binding

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-compile /wheels/*.whl \
 && rm -rf /wheels \
 # Fail the build, not the first Kerberos login, if GSSAPI is missing.
 && python -c "from gssapi.raw import inquire_sec_context_by_oid; import krb5"

COPY LICENSE NOTICE /usr/local/share/doc/dapr-smb-binding/

USER app

ENV DAPR_COMPONENTS_SOCKETS_FOLDER=/tmp/dapr-components-sockets \
    SMB_BINDING_RUNTIME_DIR=/tmp/dapr-smb-binding \
    KRB5CCNAME=MEMORY:dapr-smb-binding \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["smb-binding"]
