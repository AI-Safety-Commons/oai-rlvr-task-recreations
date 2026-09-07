FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

CMD ["internet-download", "--config", "/workspace/config.toml", "serve-search", "--host", "0.0.0.0"]
