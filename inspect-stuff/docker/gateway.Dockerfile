FROM python:3.12-slim

RUN groupadd --system gateway \
    && useradd --system --gid gateway --home /home/gateway gateway \
    && mkdir -p /home/gateway/.mitmproxy /state \
    && chown -R gateway:gateway /home/gateway /state

COPY docker/gateway-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY http_gateway /app/http_gateway
ENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
USER gateway
CMD ["mitmdump", "--listen-host", "0.0.0.0", "--listen-port", "3128", "--set", "connection_strategy=lazy", "--set", "block_global=false", "-s", "/app/http_gateway/addon.py"]
