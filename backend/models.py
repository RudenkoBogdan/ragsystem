from datetime import datetime
from sqlalchemy import Column, Float, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    papers = relationship("Paper", back_populates="owner", cascade="all, delete-orphan")
    chat_sessions = relationship("ChatSession", back_populates="owner", cascade="all, delete-orphan")
    llm_secret = relationship("UserSecret", uselist=False, cascade="all, delete-orphan")


class Paper(Base):
    __tablename__ = "papers"

    id = Column(Integer, primary_key=True, index=True)
    arxiv_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    authors = Column(String, nullable=False)
    abstract = Column(Text)
    year = Column(Integer)
    url = Column(String)
    # "processing" while the PDF is downloaded and embedded in the background,
    # then "ready" or "failed" (with a user-safe message in `error`).
    status = Column(String, nullable=False, default="ready", server_default="ready")
    error = Column(String)
    # While processing: current stage and overall progress in [0, 1].
    stage = Column(String)
    progress = Column(Float)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="papers")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, default="New Chat")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("User", back_populates="chat_sessions")
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String, nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    sources = Column(Text)  # JSON array of source references
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ChatSession", back_populates="messages")


class UserSecret(Base):
    """The user's own LLM API key, encrypted at rest (see auth/secrets.py).

    Kept server-side so the browser never has to hold it: anything readable by
    page JavaScript (localStorage) is readable by any XSS in rendered answers.
    """
    __tablename__ = "user_secrets"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    llm_api_key_encrypted = Column(Text, nullable=False)
    llm_api_key_last4 = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
