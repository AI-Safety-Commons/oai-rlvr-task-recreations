FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY WEBPAGES.md /workspace/WEBPAGES.md
