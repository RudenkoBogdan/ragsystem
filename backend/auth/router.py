from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from config import settings
from database import get_db
import models
from security.ratelimit import RateLimitExceeded, SlidingWindowLimiter, UNKNOWN_IP
from .schemas import RegisterRequest, LoginRequest, TokenResponse, UserResponse, LlmKeyRequest, LlmKeyStatus
from .secrets import set_llm_api_key, delete_llm_api_key
from .utils import hash_password, verify_password, create_token, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])

_login_account_limiter = SlidingWindowLimiter(
    settings.auth_login_rate_limit, settings.auth_login_rate_window
)
_login_ip_limiter = SlidingWindowLimiter(
    settings.auth_login_ip_rate_limit, settings.auth_login_ip_rate_window
)
_register_ip_limiter = SlidingWindowLimiter(
    settings.auth_register_rate_limit, settings.auth_register_rate_window
)


def _source_ip(request: Request) -> str:
    # The socket peer, not X-Forwarded-For: there is no trusted proxy in front
    # of the backend, so that header is attacker-chosen and would let one
    # client rotate through unlimited rate-limit buckets.
    return request.client.host if request.client else UNKNOWN_IP


def _throttle(limiter: SlidingWindowLimiter, key: str) -> None:
    try:
        limiter.check(key)
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts, try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    _throttle(_register_ip_limiter, _source_ip(request))
    if db.query(models.User).filter(models.User.username == body.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    user = models.User(username=body.username, hashed_password=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenResponse(access_token=create_token(user.id))


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    _throttle(_login_ip_limiter, _source_ip(request))
    _throttle(_login_account_limiter, body.username.strip().lower())
    user = db.query(models.User).filter(models.User.username == body.username).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=create_token(user.id))


@router.get("/me", response_model=UserResponse)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user


# The user's own LLM API key. Write-only from the client's point of view: the
# full key is never returned, only whether one is stored and its last 4 chars.
@router.get("/llm-key", response_model=LlmKeyStatus)
def get_llm_key_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    secret = db.get(models.UserSecret, current_user.id)
    if not secret:
        return LlmKeyStatus(has_key=False)
    return LlmKeyStatus(has_key=True, last4=secret.llm_api_key_last4)


@router.put("/llm-key", response_model=LlmKeyStatus)
def put_llm_key(
    body: LlmKeyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    secret = set_llm_api_key(db, current_user, body.api_key.strip())
    return LlmKeyStatus(has_key=True, last4=secret.llm_api_key_last4)


@router.delete("/llm-key", status_code=status.HTTP_204_NO_CONTENT)
def remove_llm_key(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    delete_llm_api_key(db, current_user)
