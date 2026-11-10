FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --no-create-home identity
# Image hardening: take Debian security fixes newer than the base tag, then drop the base
# image's pip (the app runs from its installed environment; pip's vendored urllib3/msgpack/
# setuptools copies are what the Trivy image scan flags).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python3 -m pip uninstall -y --disable-pip-version-check pip
USER identity
# Secrets (database URL, signing key) arrive as environment variables at runtime only.
CMD ["identity-api"]
