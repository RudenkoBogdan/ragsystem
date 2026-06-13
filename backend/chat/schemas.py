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
