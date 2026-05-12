import re
import tempfile
import arxiv
import fitz  # PyMuPDF
from vector.chroma import get_user_collection, embed
from config import settings


def parse_arxiv_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    # Match arxiv.org/abs/XXXX or arxiv.org/pdf/XXXX
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s/?#]+)", url_or_id)
    if match:
        return match.group(1).rstrip(".pdf")
    # Bare ID like "2301.07041" or "2301.07041v2"
    if re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", url_or_id):
        return url_or_id
    raise ValueError(f"Cannot parse arXiv ID from: {url_or_id}")


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += chunk_size - overlap
    return chunks


def _extract_pdf_text(pdf_path: str) -> list[tuple[int, str]]:
    """Returns list of (page_number, text) tuples."""
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text().strip()
        if text:
            pages.append((i + 1, text))
    return pages


def ingest_arxiv_paper(arxiv_id: str, user_id: int, paper_db_id: int) -> dict:
    """Download and index an arXiv paper. Returns metadata dict."""
    search = arxiv.Search(id_list=[arxiv_id], max_results=1)
    client = arxiv.Client()
    results = list(client.results(search))
    if not results:
        raise ValueError(f"arXiv paper not found: {arxiv_id}")

    paper = results[0]
    metadata = {
        "arxiv_id": arxiv_id,
        "title": paper.title,
        "authors": ", ".join(str(a) for a in paper.authors[:5]),
        "abstract": paper.summary,
        "year": paper.published.year if paper.published else None,
        "url": paper.entry_id,
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = paper.download_pdf(dirpath=tmpdir, filename="paper.pdf")
        pages = _extract_pdf_text(pdf_path)

    collection = get_user_collection(user_id)
    all_chunk_ids = []
    all_embeddings = []
    all_documents = []
    all_metadatas = []

    for page_num, page_text in pages:
        chunks = _chunk_text(page_text, settings.chunk_size, settings.chunk_overlap)
        for i, chunk in enumerate(chunks):
            chunk_id = f"paper_{paper_db_id}_p{page_num}_c{i}"
            all_chunk_ids.append(chunk_id)
            all_documents.append(chunk)
            all_metadatas.append({
                "paper_id": str(paper_db_id),
                "arxiv_id": arxiv_id,
                "title": paper.title,
                "page": page_num,
            })

    if all_documents:
        all_embeddings = embed(all_documents)
        collection.upsert(
            ids=all_chunk_ids,
            embeddings=all_embeddings,
            documents=all_documents,
            metadatas=all_metadatas,
        )

    return metadata


def delete_paper_vectors(user_id: int, paper_db_id: int) -> None:
    collection = get_user_collection(user_id)
    results = collection.get(where={"paper_id": str(paper_db_id)})
    if results["ids"]:
        collection.delete(ids=results["ids"])
