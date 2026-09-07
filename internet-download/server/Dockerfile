FROM python:3.12-alpine

RUN addgroup -S dataset && adduser -S -G dataset dataset
WORKDIR /app
COPY --chown=dataset:dataset server /app/server

ENV PYTHONDONTWRITEBYTECODE=1 \
    DATA_SERVICE_ROOT=/data \
    DATA_SERVICE_PORT=8080 \
    DATA_SERVICE_TLS_PORT=8443

USER dataset
EXPOSE 8080 8443
HEALTHCHECK --interval=5s --timeout=2s --retries=12 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
CMD ["python", "-m", "server.app"]
