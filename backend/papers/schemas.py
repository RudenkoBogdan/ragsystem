from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class AddPaperRequest(BaseModel):
    url: str


class PaperResponse(BaseModel):
    id: int
    arxiv_id: str
    title: str
    authors: str
    abstract: Optional[str] = None
    year: Optional[int] = None
    url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
