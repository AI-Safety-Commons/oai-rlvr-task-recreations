FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OVERLAY_HOST=0.0.0.0 OVERLAY_PORT=80 OVERLAY_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates
COPY static ./static
RUN mkdir -p /data
EXPOSE 80 443
HEALTHCHECK --interval=5s --timeout=2s --start-period=3s --retries=10 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1/health', timeout=1)"]
CMD ["python", "app.py"]
