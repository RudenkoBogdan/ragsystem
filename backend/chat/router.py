import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from database import get_db
from auth.utils import get_current_user
import models
from .schemas import CreateSessionRequest, SessionResponse, MessageResponse, SendMessageRequest
from .service import stream_rag_response

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    body: CreateSessionRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    session = models.ChatSession(title=body.title, user_id=current_user.id)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    return (
        db.query(models.ChatSession)
        .filter(models.ChatSession.user_id == current_user.id)
        .order_by(models.ChatSession.updated_at.desc())
        .all()
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    session = db.query(models.ChatSession).filter(
        models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()


@router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
def get_messages(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    session = db.query(models.ChatSession).filter(
        models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = (
        db.query(models.Message)
        .filter(models.Message.session_id == session_id)
        .order_by(models.Message.created_at)
        .all()
    )
    result = []
    for msg in messages:
        sources = json.loads(msg.sources) if msg.sources else []
        result.append(MessageResponse(
            id=msg.id,
            role=msg.role,
            content=msg.content,
            sources=sources,
            created_at=msg.created_at,
        ))
    return result


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: int,
    body: SendMessageRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    session = db.query(models.ChatSession).filter(
        models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Save user message
    user_msg = models.Message(session_id=session_id, role="user", content=body.content)
    db.add(user_msg)

    # Auto-title session from first message
    if session.title == "New Chat":
        session.title = body.content[:60]
    session.updated_at = datetime.utcnow()
    db.commit()

    # Build history for Claude (last 10 exchanges)
    history_rows = (
        db.query(models.Message)
        .filter(models.Message.session_id == session_id, models.Message.id != user_msg.id)
        .order_by(models.Message.created_at)
        .limit(20)
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in history_rows]

    full_response = []
    final_sources = []

    user_id = current_user.id  # Extract ID before streaming

    async def generate():
        nonlocal final_sources
        async for chunk in stream_rag_response(
            user_id,
            body.content,
            history,
            api_key=body.api_key,
            model=body.model
        ):
            yield chunk
            # Parse done event to capture sources
            if chunk.startswith("data: "):
                try:
                    event = json.loads(chunk[6:])
                    if event.get("type") == "token":
                        full_response.append(event["content"])
                    elif event.get("type") == "done":
                        final_sources = event.get("sources", [])
                except Exception:
                    pass

        # Persist assistant message after stream completes
        assistant_content = "".join(full_response)
        assistant_msg = models.Message(
            session_id=session_id,
            role="assistant",
            content=assistant_content,
            sources=json.dumps(final_sources),
        )
        db.add(assistant_msg)
        db.commit()

    return StreamingResponse(generate(), media_type="text/event-stream")
