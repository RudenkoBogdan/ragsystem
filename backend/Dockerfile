# --- build: install wheels into a venv, then drop the toolchain -------------
FROM python:3.11-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
ENV PATH=/venv/bin:$PATH

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding model (~240 MB) into the image instead of downloading it
# on the first request. Must match EMBEDDING_MODEL (config.py).
ENV FASTEMBED_CACHE_PATH=/models
RUN python -c "from fastembed import TextEmbedding; list(TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2').embed(['warmup']))"

# --- runtime ----------------------------------------------------------------
FROM python:3.11-slim

ENV PATH=/venv/bin:$PATH \
    FASTEMBED_CACHE_PATH=/models \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 1000 app
COPY --from=build /venv /venv
COPY --from=build --chown=app /models /models

WORKDIR /app
COPY --chown=app . .
RUN mkdir -p /app/data/db /app/data/chroma && chown -R app /app/data
USER app

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
