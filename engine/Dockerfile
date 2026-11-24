FROM ghcr.io/astral-sh/uv:0.13.9-python3.12-trixie-slim@sha256:b12436aceed37b21524b327362bd67e73a284b5d52f4f6be0e047dd8e4c4eb90 AS export
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --locked --offline --no-dev --format requirements.txt --no-annotate --no-header --no-emit-project -o requirements.txt \
    && uv export --locked --offline --only-group build --format requirements.txt --no-annotate --no-header -o build-requirements.txt

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=export /app/build-requirements.txt ./
RUN pip install --no-cache-dir --no-deps --require-hashes -r build-requirements.txt
COPY pyproject.toml ./
COPY afterword_engine ./afterword_engine
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation --no-index --wheel-dir /wheels .

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AFTERWORD_DB=/data/afterword.db
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=export /app/requirements.txt ./
RUN pip install --no-cache-dir --no-deps --require-hashes -r requirements.txt \
    && rm requirements.txt
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-deps --no-index --find-links /wheels bookward-engine \
    && pip check \
    && rm -rf /wheels
RUN useradd --create-home --uid 10001 afterword \
    && mkdir -p /data /backups \
    && chown -R afterword:afterword /app /data /backups
USER afterword
VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "afterword_engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
