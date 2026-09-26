from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks
from sqlalchemy.orm import Session
from database import get_db
from auth.utils import get_current_user
import models
from security.logging_setup import get_logger
from security.policy import PolicyError, resolve_page
from .schemas import AddPaperRequest, PaperResponse
from .ingest import parse_arxiv_id, ingest_arxiv_paper, delete_paper_vectors

router = APIRouter(prefix="/papers", tags=["papers"])

log = get_logger("papers.router")


@router.post("", response_model=PaperResponse, status_code=status.HTTP_201_CREATED)
def add_paper(
    body: AddPaperRequest,
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

    # Create placeholder so we have the DB id for vector metadata
    paper = models.Paper(arxiv_id=arxiv_id, title="Loading...", authors="", user_id=current_user.id)
    db.add(paper)
    db.commit()
    db.refresh(paper)

    try:
        metadata = ingest_arxiv_paper(arxiv_id, current_user.id, paper.id)
    except Exception:
        db.delete(paper)
        db.commit()
        # The traceback goes to the operator's log, the client gets a fixed
        # sentence.  The `detail` used to interpolate the exception, handing
        # the user whatever arXiv's client, PyMuPDF or the vector store
        # raised -- an information leak, and useless to them besides.
        log.exception("papers.ingest_failed arxiv_id=%s", arxiv_id)
        raise HTTPException(
            status_code=500,
            detail="Ingestion failed: the paper could not be retrieved from arXiv.",
        )

    paper.title = metadata["title"]
    paper.authors = metadata["authors"]
    paper.abstract = metadata["abstract"]
    paper.year = metadata["year"]
    paper.url = metadata["url"]
    db.commit()
    db.refresh(paper)
    return paper


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
