import hashlib
import os
import threading
from typing import Optional

import chromadb
import numpy as np
from chromadb.config import Settings as ChromaSettings
from fastembed import TextEmbedding

from config import settings

os.makedirs(settings.chroma_path, exist_ok=True)

_chroma_client: Optional[chromadb.PersistentClient] = None
# Multilingual sentence-transformers model on onnxruntime (via fastembed), so
# a Russian question finds the English passage that answers it. No torch.
_embedding_model: Optional[TextEmbedding] = None
_embedding_lock = threading.Lock()


def get_chroma_client() -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(
            path=settings.chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    return _chroma_client


def get_embedding_model() -> TextEmbedding:
    global _embedding_model
    with _embedding_lock:
        if _embedding_model is None:
            _embedding_model = TextEmbedding(settings.embedding_model)
    return _embedding_model


# HNSW defaults (search_ef=10) are tuned for huge collections and here miss
# the true nearest chunk now and then, differently from process to process.
# Libraries are small, so near-exhaustive search costs nothing. Chroma only
# accepts these at creation, which is why they are part of the tag below.
HNSW_PARAMS = {"hnsw:search_ef": 200, "hnsw:construction_ef": 200}


def model_tag(model_name: str) -> str:
    key = f"{model_name}|ef={HNSW_PARAMS['hnsw:search_ef']}"
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def collection_name(user_id: int, model_name: Optional[str] = None) -> str:
    # Vectors from different models are not comparable, so each model gets its
    # own collection; switching models re-ingests into a fresh one (papers.router.reindex_stale_papers).
    return f"user_{user_id}_{model_tag(model_name or settings.embedding_model)}"


def get_user_collection(user_id: int) -> chromadb.Collection:
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=collection_name(user_id),
        metadata={
            "hnsw:space": "cosine",
            **HNSW_PARAMS,
            "embedding_model": settings.embedding_model,
        },
    )


def embed(texts: list[str]) -> list[list[float]]:
    vectors = np.array(list(get_embedding_model().embed(texts)), dtype=np.float32)
    # This model's output is not unit-length; normalise so scores are cosines.
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True).clip(min=1e-12)
    return vectors.tolist()
