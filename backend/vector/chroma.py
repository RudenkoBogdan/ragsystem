import os
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

from config import settings

os.makedirs(settings.chroma_path, exist_ok=True)

_chroma_client: Optional[chromadb.PersistentClient] = None
# all-MiniLM-L6-v2 on onnxruntime, bundled with chromadb: the same model the
# app used through sentence-transformers, without pulling in torch.
_embedding_fn: Optional[ONNXMiniLM_L6_V2] = None


def get_chroma_client() -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(
            path=settings.chroma_path,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    return _chroma_client


def get_embedding_fn() -> ONNXMiniLM_L6_V2:
    global _embedding_fn
    if _embedding_fn is None:
        _embedding_fn = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
    return _embedding_fn


def get_user_collection(user_id: int) -> chromadb.Collection:
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=f"user_{user_id}",
        metadata={"hnsw:space": "cosine"},
    )


def embed(texts: list[str]) -> list[list[float]]:
    return [list(map(float, v)) for v in get_embedding_fn()(texts)]
