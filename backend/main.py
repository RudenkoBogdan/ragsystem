"""ASGI entry point.

Import order here is load-bearing, not alphabetical.  ``configure_logging`` is
the first statement so that the two boot checks immediately below it have a
logger to report through, and both of them run *before*
``Base.metadata.create_all`` and before the app object exists: a deployment
that would sign tokens with the publicly-readable placeholder ``JWT_SECRET``
must fail before it opens a database, not after.

Nothing in this file ever prints a secret.  The boot check reports the
*variable* name and the *rule* it broke; the value stays where it is.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import Base, engine
from security.logging_setup import configure_logging, get_logger
from security.policy import PolicyError, jwt_secret_problem, parse_cors_origins

from auth.router import router as auth_router
from papers.router import router as papers_router
from chat.router import router as chat_router

configure_logging(settings.log_level)
log = get_logger("main")

# --- SEC-2: refuse to run with a placeholder or short JWT signing key -------
# The default in config.py is "change-me-in-production", which is public in
# this repository: anyone could mint a token for any `sub` and the app would
# accept it as a valid session. There is no in-band way to report that to a
# user who has not noticed, so the process does not start. Set
# ALLOW_INSECURE_JWT_SECRET=true to opt out for local development.
_jwt_problem = jwt_secret_problem(
    settings.jwt_secret, allow_insecure=settings.allow_insecure_jwt_secret
)
if _jwt_problem:
    log.critical("startup.jwt_secret_invalid reason=%s", _jwt_problem)
    raise SystemExit(
        "Refusing to start: JWT_SECRET %s. Generate one with "
        '`python3 -c "import secrets;print(secrets.token_urlsafe(48))"` '
        "and set it in .env." % _jwt_problem
    )

# --- CORS: an explicit allow-list, parsed and bounded before it is installed -
_origins: list = []
try:
    _origins = parse_cors_origins(
        settings.cors_allow_origins, allow_credentials=settings.cors_allow_credentials
    )
except PolicyError as exc:
    log.critical("startup.cors_invalid reason=%s", exc)
    raise SystemExit("Refusing to start: %s" % exc)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="RAG Research Assistant", version="1.0.0")

if _origins:
    # The middleware used to accept a wildcard origin together with
    # allow_credentials=True, a wildcard method list and a wildcard header
    # list. That combination let any web page the user visited issue
    # authenticated requests to this API and read the responses, and it
    # advertised every method and header in the preflight. Now: named origins,
    # named methods, named headers, a 10-minute preflight cache, and no
    # middleware at all when the list is empty.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    log.info(
        "startup.cors origins=%d credentials=%s",
        len(_origins),
        settings.cors_allow_credentials,
    )
else:
    # Not an error: a same-origin deployment (frontend served behind the same
    # host) needs no CORS middleware, and a silent browser rejection is far
    # harder to diagnose than one line at startup.
    log.warning(
        "startup.cors disabled: CORS_ALLOW_ORIGINS is empty, so no cross-origin "
        "browser request will be permitted"
    )

app.include_router(auth_router, prefix="/api")
app.include_router(papers_router, prefix="/api")
app.include_router(chat_router, prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok"}
