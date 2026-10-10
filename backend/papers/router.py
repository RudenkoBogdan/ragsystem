import time
from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks
from sqlalchemy.orm import Session
from database import SessionLocal, get_db
from auth.utils import get_current_user
import models
from security.logging_setup import get_logger
from security.policy import PolicyError, resolve_page
from .schemas import AddPaperRequest, PaperResponse
from .ingest import parse_arxiv_id, ingest_arxiv_paper, delete_paper_vectors

router = APIRouter(prefix="/papers", tags=["papers"])

log = get_logger("papers.router")


@router.post("", response_model=PaperResponse, status_code=status.HTTP_202_ACCEPTED)
def add_paper(
    body: AddPaperRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    try:
        arxiv_id = parse_arxiv_id(body.url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    existing = (
        db.query(models.Paper)
        .filter(models.Paper.arxiv_id == arxiv_id, models.Paper.user_id == current_user.id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail="Paper already in your library")

    # Create the row now (its id goes into the vector metadata) and do the slow
    # part -- arXiv lookup, PDF download, embedding -- after the response.
    paper = models.Paper(
        arxiv_id=arxiv_id,
        title=arxiv_id,
        authors="",
        user_id=current_user.id,
        status="processing",
        stage="queued",
        progress=0.0,
    )
    db.add(paper)
    db.commit()
    db.refresh(paper)

    background_tasks.add_task(_ingest_in_background, paper.id, arxiv_id, current_user.id)
    return paper


def _ingest_in_background(paper_id: int, arxiv_id: str, user_id: int) -> None:
    # Sync function: Starlette runs it in the threadpool, so the event loop is
    # free while the PDF downloads and the embedding model runs.
    db = SessionLocal()
    try:
        last_write = 0.0

        def on_progress(stage: str, fraction: float) -> None:
            # Throttled: the client polls about once a second, so writing on
            # every 64 KB block would only churn SQLite.
            nonlocal last_write
            now = time.monotonic()
            if now - last_write < 0.5 and fraction < 1:
                return
            last_write = now
            db.query(models.Paper).filter(models.Paper.id == paper_id).update(
                {"stage": stage, "progress": round(fraction, 3)}
            )
            db.commit()

        try:
            metadata = ingest_arxiv_paper(arxiv_id, user_id, paper_id, on_progress)
        except Exception:
            # The traceback goes to the operator's log; the client gets a fixed
            # sentence, never the text of whatever arXiv/PyMuPDF/Chroma raised.
            log.exception("papers.ingest_failed arxiv_id=%s", arxiv_id)
            paper = db.get(models.Paper, paper_id)
            if paper:
                paper.status = "failed"
                paper.error = "Ingestion failed: the paper could not be retrieved from arXiv."
                db.commit()
            return

        paper = db.get(models.Paper, paper_id)
        if paper is None:
            # Deleted while it was being ingested: drop the vectors just written.
            delete_paper_vectors(user_id, paper_id)
            return
        paper.title = metadata["title"]
        paper.authors = metadata["authors"]
        paper.abstract = metadata["abstract"]
        paper.year = metadata["year"]
        paper.url = metadata["url"]
        paper.status = "ready"
        paper.error = None
        paper.stage = None
        paper.progress = None
        db.commit()
    finally:
        db.close()


def mark_interrupted_ingests_failed() -> None:
    """Background tasks die with the process; don't leave rows spinning forever."""
    db = SessionLocal()
    try:
        db.query(models.Paper).filter(models.Paper.status == "processing").update(
            {"status": "failed", "error": "Ingestion was interrupted. Delete and add it again."}
        )
        db.commit()
    finally:
        db.close()


@router.get("", response_model=list[PaperResponse])
def list_papers(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    limit: int = Query(default=200, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # SEC-6: bounded and ordered.  The response shape is unchanged -- a bare
    # JSON array -- but it can no longer be the whole table.
    try:
        effective_limit, effective_offset = resolve_page(
            limit, offset, max_limit=200, default_limit=200
        )
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return list(
        db.query(models.Paper)
        .filter(models.Paper.user_id == current_user.id)
        # An explicit order, so paging through the library with `offset`
        # cannot repeat or skip a row.
        .order_by(models.Paper.id)
        .limit(effective_limit)
        .offset(effective_offset)
    )


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_paper(
    paper_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    paper = db.query(models.Paper).filter(
        models.Paper.id == paper_id, models.Paper.user_id == current_user.id
    ).first()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    delete_paper_vectors(current_user.id, paper_id)
    db.delete(paper)
    db.commit()
