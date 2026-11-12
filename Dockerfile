# API image. The database is always Neon (configured via .env); no DB runs in this container.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf

WORKDIR /srv
# CPU-only torch keeps the image ~1.5 GB smaller; the 384-d bge model is fast enough on CPU.
COPY requirements.lock ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple -c requirements.lock torch
COPY pyproject.toml README.md ./
COPY app ./app
COPY eval ./eval
COPY scripts ./scripts
RUN pip install -c requirements.lock .

# Bake the embedding model into the image so cold starts don't download it.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from sentence_transformers import SentenceTransformer as S; S('${EMBEDDING_MODEL}')"

COPY alembic.ini ./
COPY migrations ./migrations
COPY data ./data

RUN useradd --create-home --uid 10001 app && chown -R app /srv /opt/hf
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
