FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    STACK_HOST=0.0.0.0 STACK_PORT=80 STACK_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py importer.py inspect_stackexchange.py ./
COPY templates ./templates
COPY static ./static
RUN mkdir -p /data /archive
EXPOSE 80
HEALTHCHECK --interval=5s --timeout=2s --start-period=3s --retries=10 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1/health', timeout=1)"]
CMD ["python", "app.py"]
