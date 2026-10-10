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
from vector.chroma import collection_name, get_chroma_client, get_user_collection

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


INTERRUPTED_ERROR = "Ingestion was interrupted. Delete and add it again."


def mark_interrupted_ingests_failed() -> None:
    """Background tasks die with the process; don't leave rows spinning forever."""
    db = SessionLocal()
    try:
        db.query(models.Paper).filter(models.Paper.status == "processing").update(
            {"status": "failed", "error": INTERRUPTED_ERROR}
        )
        db.commit()
    finally:
        db.close()


def _has_current_vectors(user_id: int, paper_id: int) -> bool:
    found = get_user_collection(user_id).get(where={"paper_id": str(paper_id)}, limit=1)
    return bool(found["ids"])


def reindex_stale_papers() -> None:
    """Re-ingest papers that have no vectors from the current embedding model.

    Runs once at startup in a background thread (after a model change, or for
    a library indexed before per-model collections existed). Papers are
    re-downloaded rather than re-embedded from stored chunks, so they are also
    re-chunked to fit the new model's input limit. Sequential on purpose:
    arXiv rate-limits, and the embedding model already uses every core.
    """
    db = SessionLocal()
    try:
        stale = [
            (p.id, p.arxiv_id, p.user_id)
            for p in db.query(models.Paper)
            .filter(
                # Interrupted ones are retried too: a restart during a reindex
                # must not leave those papers failed for good.
                (models.Paper.status == "ready")
                | ((models.Paper.status == "failed") & (models.Paper.error == INTERRUPTED_ERROR))
            )
            .order_by(models.Paper.id)
            if not _has_current_vectors(p.user_id, p.id)
        ]
        if stale:
            log.info("papers.reindex_start count=%d", len(stale))
            # Mark them all first so the UI shows the whole queue at once.
            db.query(models.Paper).filter(models.Paper.id.in_([s[0] for s in stale])).update(
                {"status": "processing", "stage": "queued", "progress": 0.0},
                synchronize_session=False,
            )
            db.commit()
        user_ids = {u for (u,) in db.query(models.Paper.user_id).distinct()}
    finally:
        db.close()

    for paper_id, arxiv_id, user_id in stale:
        _ingest_in_background(paper_id, arxiv_id, user_id)

    # Vectors from other models are unusable now; drop their collections.
    client = get_chroma_client()
    for user_id in user_ids:
        current = collection_name(user_id)
        for col in client.list_collections():
            name = col if isinstance(col, str) else col.name
            if name != current and (name == f"user_{user_id}" or name.startswith(f"user_{user_id}_")):
                client.delete_collection(name)
                log.info("papers.reindex_dropped_collection name=%s", name)


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
