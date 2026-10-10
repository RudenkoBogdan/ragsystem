import os
import re
import tempfile
import urllib.request
from typing import Callable, Optional
import arxiv
import fitz  # PyMuPDF
from vector.chroma import get_user_collection, embed
from config import settings


_NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
_OLD_ID = r"[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"  # e.g. hep-th/9901001


def parse_arxiv_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()

    # Match arxiv.org/abs/XXXX or arxiv.org/pdf/XXXX[.pdf]
    match = re.search(rf"arxiv\.org/(?:abs|pdf)/({_NEW_ID}|{_OLD_ID})(?:\.pdf)?(?:$|[?#/\s])", url_or_id)
    if match:
        return match.group(1)

    # Bare ID like "2301.07041", "2301.07041v2" or "hep-th/9901001"
    if re.fullmatch(rf"{_NEW_ID}|{_OLD_ID}", url_or_id):
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


# on_progress(stage, fraction): fraction is overall progress in [0, 1].
ProgressFn = Callable[[str, float], None]

# Share of the overall bar each stage owns, in order. Embedding dominates on
# CPU; the download depends on the network.
_STAGES = [
    ("metadata", 0.05),
    ("downloading", 0.30),
    ("extracting", 0.10),
    ("embedding", 0.50),
    ("saving", 0.05),
]
_STAGE_START = {}
_acc = 0.0
for _name, _share in _STAGES:
    _STAGE_START[_name] = (_acc, _share)
    _acc += _share

EMBED_BATCH = 32
DOWNLOAD_BLOCK = 64 * 1024


def _reporter(on_progress: Optional[ProgressFn]) -> Callable[[str, float], None]:
    def report(stage: str, within: float) -> None:
        if on_progress is None:
            return
        start, share = _STAGE_START[stage]
        on_progress(stage, start + share * max(0.0, min(1.0, within)))
    return report


def _download(url: str, path: str, report: Callable[[str, float], None]) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ragsystem/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(path, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while block := resp.read(DOWNLOAD_BLOCK):
            out.write(block)
            done += len(block)
            # Without Content-Length, creep toward the end of the stage instead.
            report("downloading", done / total if total else 1 - 1 / (1 + done / 2_000_000))


def _extract_pdf_text(pdf_path: str, report: Callable[[str, float], None]) -> list[tuple[int, str]]:
    """Returns list of (page_number, text) tuples."""
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text().strip()
        if text:
            pages.append((i + 1, text))
        report("extracting", (i + 1) / doc.page_count)
    return pages


def ingest_arxiv_paper(
    arxiv_id: str, user_id: int, paper_db_id: int, on_progress: Optional[ProgressFn] = None
) -> dict:
    """Download and index an arXiv paper. Returns metadata dict."""
    report = _reporter(on_progress)
    report("metadata", 0)
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
    report("metadata", 1)

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "paper.pdf")
        _download(paper.pdf_url, pdf_path, report)
        pages = _extract_pdf_text(pdf_path, report)

    collection = get_user_collection(user_id)
    all_chunk_ids = []
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
        # In batches, so there is something to report between them.
        all_embeddings = []
        for i in range(0, len(all_documents), EMBED_BATCH):
            all_embeddings.extend(embed(all_documents[i:i + EMBED_BATCH]))
            report("embedding", min(i + EMBED_BATCH, len(all_documents)) / len(all_documents))
        report("saving", 0)
        collection.upsert(
            ids=all_chunk_ids,
            embeddings=all_embeddings,
            documents=all_documents,
            metadatas=all_metadatas,
        )
    report("saving", 1)

    return metadata


def delete_paper_vectors(user_id: int, paper_db_id: int) -> None:
    collection = get_user_collection(user_id)
    results = collection.get(where={"paper_id": str(paper_db_id)})
    if results["ids"]:
        collection.delete(ids=results["ids"])
