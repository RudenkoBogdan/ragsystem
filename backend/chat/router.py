import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from database import get_db
from auth.utils import get_current_user
import models
from security.logging_setup import get_logger
from security.netguard import (
    NetGuardError,
    UpstreamProviderError,
    public_error_message,
)
from security.policy import PolicyError, resolve_page
from .schemas import CreateSessionRequest, SessionResponse, MessageResponse, SendMessageRequest
from .service import stream_rag_response
from .citations import abstention_message

router = APIRouter(prefix="/chat", tags=["chat"])

log = get_logger("chat.router")


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
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # SEC-6: bounded and ordered.  `resolve_page` is the policy; FastAPI's
    # `ge`/`le` are the fast path that turns an out-of-range value into a 422
    # before the handler runs, and the `except` is the belt-and-braces case
    # where the defaults in this signature and the policy ever disagree.
    try:
        effective_limit, effective_offset = resolve_page(
            limit, offset, max_limit=200, default_limit=200
        )
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return list(
        db.query(models.ChatSession)
        .filter(models.ChatSession.user_id == current_user.id)
        # `id` breaks `updated_at` ties: two sessions renamed in the same
        # tick share a timestamp, and without a tiebreaker "the first 200" is
        # not a stable set of rows.
        .order_by(models.ChatSession.updated_at.desc(), models.ChatSession.id.desc())
        .limit(effective_limit)
        .offset(effective_offset)
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
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
):
    session = db.query(models.ChatSession).filter(
        models.ChatSession.id == session_id, models.ChatSession.user_id == current_user.id
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        effective_limit, effective_offset = resolve_page(
            limit, offset, max_limit=200, default_limit=200
        )
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # `id` breaks ties in both directions: `created_at` is set from
    # datetime.utcnow() at flush time, so two messages saved in the same tick
    # can share a value and render out of order. `id` is monotonic per insert.
    if order == "desc":
        # Last-page idiom. `LIMIT/OFFSET` count from the start of the
        # ordering, so "the newest 20" is unreachable by ordering ascending
        # and slicing -- it has to be selected descending and then restored
        # to chronological order for the client. `list(...)` around the
        # bounded Query is how SQLAlchemy materialises it.
        newest_first = list(
            db.query(models.Message)
            .filter(models.Message.session_id == session_id)
            .order_by(models.Message.created_at.desc(), models.Message.id.desc())
            .limit(effective_limit)
            .offset(effective_offset)
        )
        messages = list(reversed(newest_first))
    else:
        messages = list(
            db.query(models.Message)
            .filter(models.Message.session_id == session_id)
            .order_by(models.Message.created_at, models.Message.id)
            .limit(effective_limit)
            .offset(effective_offset)
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

    # Build history for the model (the last 10 exchanges).
    # Ordering ascending and then limiting took the OLDEST 20 messages of the
    # session, not the newest -- the opposite of what the comment claims, and
    # most visible once questions can be scoped to different papers. Select
    # descending, then restore chronological order for the prompt.
    history_rows = (
        db.query(models.Message)
        .filter(models.Message.session_id == session_id, models.Message.id != user_msg.id)
        .order_by(models.Message.id.desc())
        .limit(20)
        .all()
    )
    history = [{"role": m.role, "content": m.content} for m in reversed(history_rows)]

    # Resolve scope titles for the answer's "searched only" readout and for the
    # abstention message. The user_id filter is mandatory: without it, a caller
    # could name someone else's paper id and have its title echoed back. Unknown
    # ids (deleted mid-session, or another user's) are simply dropped rather than
    # rejected -- that race is normal, not a client error.
    scope_titles = []
    if body.paper_ids:
        rows = (
            db.query(models.Paper)
            .filter(
                models.Paper.user_id == current_user.id,
                models.Paper.id.in_(body.paper_ids),
            )
            .order_by(models.Paper.id)
            .all()
        )
        scope_titles = [row.title for row in rows if row.title]

    full_response = []
    final_sources = []
    # Set by the terminal `done` event, and the ONLY source of truth for whether
    # the server abstained. It must not be inferred from "no tokens arrived",
    # because a provider that 401s also produces no tokens -- and recording that
    # as "I found nothing in your library" would persist a confident falsehood.
    final_abstained = False
    stream_failed = False

    user_id = current_user.id  # Extract ID before streaming
    scope = {
        "applied": bool(body.paper_ids),
        "paper_ids": list(body.paper_ids or []),
        "paper_titles": scope_titles,
    }

    async def generate():
        nonlocal final_sources, final_abstained, stream_failed
        try:
            async for chunk in stream_rag_response(
                user_id,
                body.content,
                history,
                paper_ids=body.paper_ids,
                api_key=body.api_key,
                model=body.model,
                provider=body.provider,
                base_url=body.base_url,
                scope_titles=scope_titles,
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
                            final_abstained = event.get("abstained") is True
                    except Exception:
                        pass
        except Exception as exc:  # noqa: BLE001 - the stream must always terminate
            stream_failed = True
            # A failure inside the generator (LLM refused the connection, bad
            # provider URL, embedding model not loaded) used to end the response
            # with no `done` event at all. The client only clears its streaming
            # state in `onDone`/`onError`, so the composer stayed disabled until
            # the page was reloaded. Emit a terminal `done` carrying the error so
            # the client always gets exactly one way out.
            #
            # SEC-1: `str(exc)` is no longer sent.  The service raises
            # `UpstreamProviderError` carrying a redacted URL and body, and
            # `NetGuardError` carrying only a code; `public_error_message` is
            # the one sink, so what reaches the client is a fixed sentence and
            # the detail an operator needs goes to the log instead.  This is
            # what stopped the outbound guard from being a *read* oracle
            # against internal services.
            if isinstance(exc, NetGuardError):
                log.warning("llm.guard_rejected code=%s", exc.code)
            elif isinstance(exc, UpstreamProviderError):
                log.error(
                    "llm.upstream_error status=%s url=%s body=%s",
                    exc.status,
                    exc.safe_url,
                    exc.safe_body,
                )
            else:
                log.exception("llm.stream_failed")
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "done",
                        "sources": final_sources,
                        "cited": [],
                        "unresolved": [],
                        "scope": scope,
                        "coverage": None,
                        "abstained": False,
                        "error": public_error_message(exc),
                    }
                )
                + "\n\n"
            )

        # Persist assistant message after stream completes
        assistant_content = "".join(full_response)
        if stream_failed:
            # A failed request is not a failed retrieval. The client already
            # shows the error inline; persisting only the partial answer would
            # make a truncated reply read as a complete one after a reload, so
            # the same caveat is written into the stored message too.
            assistant_content = (
                f"{assistant_content}\n\n_The request failed before this answer "
                f"finished._" if assistant_content else
                "_The request failed before an answer could be generated._"
            )
        elif final_abstained and not assistant_content:
            # The honest-refusal path. It normally already streamed its sentence;
            # this is the belt-and-braces case where it somehow did not.
            assistant_content = abstention_message(scope)
        elif not assistant_content:
            assistant_content = (
                "_No answer was generated for this question._"
            )
        assistant_msg = models.Message(
            session_id=session_id,
            role="assistant",
            content=assistant_content,
            sources=json.dumps(final_sources),
        )
        db.add(assistant_msg)
        db.commit()

    return StreamingResponse(generate(), media_type="text/event-stream")
