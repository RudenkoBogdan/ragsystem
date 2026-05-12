import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Load .env from project root (parent of backend directory)
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    # Also set in os.environ to ensure pydantic sees it
    content = env_path.read_text()
    for line in content.strip().split("\n"):
        if "=" in line and not line.startswith("#"):
            key, val = line.split("=", 1)
            os.environ[key.strip()] = val.strip()

# Use Docker paths if running in container, otherwise use local paths
if os.path.exists("/.dockerenv"):
    DEFAULT_DB_PATH = "/app/data/db/ragsystem.db"
    DEFAULT_CHROMA_PATH = "/app/data/chroma"
else:
    DEFAULT_DB_PATH = "./data/db/ragsystem.db"
    DEFAULT_CHROMA_PATH = "./data/chroma"


class Settings(BaseSettings):
    anthropic_api_key: str
    claude_model: str = "anthropic/claude-3-5-sonnet"
    openrouter_base_url: str = "https://openrouter.io/api/v1"
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_days: int = 7
    db_path: str = DEFAULT_DB_PATH
    chroma_path: str = DEFAULT_CHROMA_PATH
    embedding_model: str = "all-MiniLM-L6-v2"
    rag_top_k: int = 5
    chunk_size: int = 512
    chunk_overlap: int = 64

    class Config:
        env_file = str(env_path)


settings = Settings()
