FROM python:3.11-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global @openai/codex@0.153.4 \
    && codex --version

RUN python -m pip install --no-cache-dir 'pyte>=0.8,<0.9'

WORKDIR /canary
