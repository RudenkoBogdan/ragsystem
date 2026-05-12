from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from database import Base, engine
from auth.router import router as auth_router
from papers.router import router as papers_router
from chat.router import router as chat_router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="RAG Research Assistant", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(papers_router, prefix="/api")
app.include_router(chat_router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}
