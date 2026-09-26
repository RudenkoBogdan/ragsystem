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
    anthropic_api_key: str = ""
    claude_model: str = "anthropic/claude-3-5-sonnet"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Local model provider (e.g. Ollama via its OpenAI-compatible endpoint)
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3"
    # Default provider: "openrouter" or "ollama"
    llm_provider: str = "openrouter"
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_days: int = 7
    db_path: str = DEFAULT_DB_PATH
    chroma_path: str = DEFAULT_CHROMA_PATH
    embedding_model: str = "all-MiniLM-L6-v2"
    rag_top_k: int = 5
    chunk_size: int = 512
    chunk_overlap: int = 64

    # --- security (added by the security-hardening series) -------------------
    # Every default below is the SAFE one. None of them weakens anything that
    # already worked; they only stop a deployment from silently keeping a
    # state the auditor flagged as exploitable.

    # Local-development escape hatch for the boot check in main.py. While this
    # is false the process refuses to start with the shipped placeholder
    # JWT_SECRET, so a deployment cannot sign tokens with a constant that is
    # public in this repository. Never set it true in production.
    allow_insecure_jwt_secret: bool = False

    # Whether a user-supplied LLM base_url may resolve to loopback, link-local
    # or private addresses. False removes the authenticated-SSRF read-oracle
    # against internal services that any registered account could otherwise
    # aim the server's HTTP client at.
    llm_allow_private_hosts: bool = False

    # Comma-separated list of browser origins permitted by CORS. Replaces the
    # previous allow_origins=["*"], which let any web page on the internet read
    # authenticated API responses on the user's behalf.
    cors_allow_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # Only ever true alongside an explicit origin list. Browsers refuse
    # "Access-Control-Allow-Origin: *" on a credentialed request anyway, so
    # the old wildcard-plus-credentials pair was misleading as well as unsafe.
    cors_allow_credentials: bool = False

    # Root level of the `ragapp` logger. DEBUG is the useful value when
    # diagnosing retrieval or SSE problems; INFO is quiet enough to ship.
    log_level: str = "INFO"

    # Login throttle, per account. Bounds both the argon2 CPU-burn oracle and
    # the offline-cracking oracle that an unauthenticated login endpoint is.
    auth_login_rate_limit: int = 10
    auth_login_rate_window: int = 60
    # Login throttle, per source IP. Stops one attacker spraying many accounts
    # from a single host, which the per-account limit alone would not notice.
    auth_login_ip_rate_limit: int = 30
    auth_login_ip_rate_window: int = 60
    # Registration throttle, per source IP. Self-registration is how an
    # attacker gets the account they need to attack anything else here.
    auth_register_rate_limit: int = 5
    auth_register_rate_window: int = 300

    class Config:
        env_file = str(env_path)


settings = Settings()
