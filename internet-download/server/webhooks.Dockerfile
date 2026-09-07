FROM python:3.12-alpine

RUN addgroup -S webhooks && adduser -S -G webhooks webhooks
WORKDIR /app
COPY server/webhooks.py /app/webhooks.py
RUN pip install --no-cache-dir Flask
ENV PYTHONDONTWRITEBYTECODE=1 WEBHOOK_PORT=80 WEBHOOK_EVENTS=/data/events.jsonl
RUN mkdir /data && chown webhooks:webhooks /data
USER webhooks
EXPOSE 80
HEALTHCHECK --interval=5s --timeout=2s --retries=12 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost/health')"
CMD ["python", "/app/webhooks.py"]
