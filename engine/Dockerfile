FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AFTERWORD_DB=/data/afterword.db
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY afterword_engine ./afterword_engine
RUN pip install --no-cache-dir .
RUN useradd --create-home --uid 10001 afterword \
    && mkdir -p /data /backups \
    && chown -R afterword:afterword /app /data /backups
USER afterword
VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "afterword_engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
