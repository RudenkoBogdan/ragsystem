from config import settings
from security.logging_setup import configure_logging

# First, so that import-time problems below are logged in the final format.
configure_logging(settings.log_level)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine
from auth.router import router as auth_router
from papers.router import router as papers_router
from chat.router import router as chat_router
from security.policy import jwt_secret_problem, parse_cors_origins

# Refuse to sign tokens with the placeholder secret shipped in this repository.
_jwt_problem = jwt_secret_problem(
    settings.jwt_secret, allow_insecure=settings.allow_insecure_jwt_secret
)
if _jwt_problem:
    raise RuntimeError(
        f"Refusing to start: {_jwt_problem} "
        "Set a strong JWT_SECRET (or ALLOW_INSECURE_JWT_SECRET=true for local dev only)."
    )

Base.metadata.create_all(bind=engine)

app = FastAPI(title="RAG Research Assistant", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins(
        settings.cors_allow_origins,
        allow_credentials=settings.cors_allow_credentials,
    ),
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(papers_router, prefix="/api")
app.include_router(chat_router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}
