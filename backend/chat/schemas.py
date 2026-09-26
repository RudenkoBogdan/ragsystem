from datetime import datetime
from pydantic import BaseModel
from typing import Optional


class CreateSessionRequest(BaseModel):
    title: str = "New Chat"


class SessionResponse(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SourceRef(BaseModel):
    title: str
    arxiv_id: str
    page: int
    # Verifiable citations. All optional (default None) so that message rows
    # persisted before this feature still load: pydantic drops unknown keys and
    # these four would silently vanish on page refresh.
    label: Optional[int] = None
    snippet: Optional[str] = None
    score: Optional[float] = None
    chunk_count: Optional[int] = None


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    sources: list[SourceRef] = []
    created_at: datetime

    class Config:
        from_attributes = True


class SendMessageRequest(BaseModel):
    content: str
    api_key: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None  # "openrouter" | "ollama"
    base_url: Optional[str] = None
    # Restrict retrieval to these papers. Optional and defaulting to None, so an
    # existing client that omits it keeps searching the whole library and the
    # request is byte-identical to before. An empty list is treated the same as
    # None on purpose: the UI never renders a scope chip for an empty selection,
    # so "no ids" can only mean "no scope".
    paper_ids: Optional[list[int]] = None
