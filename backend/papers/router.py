from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from database import get_db
from auth.utils import get_current_user
import models
from .schemas import AddPaperRequest, PaperResponse
from .ingest import parse_arxiv_id, ingest_arxiv_paper, delete_paper_vectors

router = APIRouter(prefix="/papers", tags=["papers"])


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
    except Exception as e:
        db.delete(paper)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {e}")

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
):
    return db.query(models.Paper).filter(models.Paper.user_id == current_user.id).all()


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
