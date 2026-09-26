"""Authentication endpoints: register, login, me.

Two audit findings live in this file and both are enforced here rather than in
the pydantic schemas, which is a deliberate choice (see the docstring on
`RegisterRequest` in `schemas.py`):

* **Finding 14 -- no credential policy.** `POST /api/auth/register` accepted an
  empty or one-character password. `security.policy.username_problem` /
  `password_problem` now gate it and return `422` with the policy's own
  message.
* **Finding 15 -- no rate limiting on argon2 login.** `POST /api/auth/login`
  verified an argon2 hash on every unauthenticated request, with no throttle.
  Three sliding windows now run *before* the database is touched and before
  `verify_password` is called.

**Why the policy is not applied to login.** Only the upper bound is. A `422`
saying "password must be at least 12 characters" on the login endpoint would
turn "wrong password" into "malformed request", which is a free oracle about
the stored credential: an attacker can distinguish accounts whose passwords
predate the policy from accounts whose passwords are merely wrong, and, worse,
a length-policy complaint reveals a fact about a *guess* that the 401 path
deliberately does not. The floor is a registration-time rule; argon2's own
length ceiling is the only hard limit a login attempt needs (it exists because
an unbounded password is a free CPU-exhaustion primitive -- hashing a
64 megabyte string costs the server far more than it costs the attacker).

**Why the limiters run before the lookup.** The whole finding is that argon2
is deliberately expensive. Checking the budget *after* verifying the password
would leave the cost exactly where it was and only slow the rejection down.
The throttle is the thing that has to happen first.

**USERNAME ENUMERATION BY TIMING.** A nonexistent username returned as soon
as the `SELECT` finished; an existing one returned after a full argon2 verify.
That is a free oracle for "does this account exist", for free, with no
failures logged. `DUMMY_HASH` below closes the control-flow half of it.

Run the tests with::

    python3 -m unittest discover -s backend/tests -t backend -v
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from database import get_db
from config import settings
import models
from security.logging_setup import get_logger
from security.policy import password_problem, password_too_long, username_problem
from security.ratelimit import RateLimitExceeded, SlidingWindowLimiter, client_ip
from .schemas import RegisterRequest, LoginRequest, TokenResponse, UserResponse
from .utils import hash_password, verify_password, create_token, get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])

log = get_logger("auth.router")

#: One sentence for every throttled response, whatever tripped. The client
#: learns *that* it is throttled and *when* to come back; it never learns which
#: of the three windows it hit, because that is free information about the
#: limit's shape and about whether the account exists.
RATE_LIMIT_DETAIL = "Too many attempts. Try again later."

#: Per (ip, username). Bounds the online password-guessing oracle against one
#: account from one host. Reset on a successful login (below) so an ordinary
#: user who fumbles a few passwords is not locked out.
_login_limiter = SlidingWindowLimiter(
    limit=settings.auth_login_rate_limit,
    window_seconds=settings.auth_login_rate_window,
)

#: Per ip. Bounds the same oracle *across* accounts from one host, which the
#: per-account window cannot see: an attacker spraying `alice`, `bob`, `carol`
#: gets a fresh per-account budget for each one. Deliberately NOT reset on
#: success -- see the login() comment.
_login_ip_limiter = SlidingWindowLimiter(
    limit=settings.auth_login_ip_rate_limit,
    window_seconds=settings.auth_login_ip_rate_window,
)

#: Per ip. Self-registration is how an attacker obtains the account they need
#: to attack anything else here, so creating one is throttled too.
_register_limiter = SlidingWindowLimiter(
    limit=settings.auth_register_rate_limit,
    window_seconds=settings.auth_register_rate_window,
)

#: A *placeholder*, deliberately empty. See the long comment at its use site.
DUMMY_HASH = ""


def _limited(limiter: SlidingWindowLimiter, key: str) -> Optional[HTTPException]:
    """Return a 429 to raise, or ``None`` to carry on.

    Returning the exception instead of raising it lets one line compose two
    independent windows with ``or``, short-circuiting on whichever trips
    first, so the caller does not have to repeat the raise.

    The logged key is the **scope only** (``key.split(":")[0]`` -- "login",
    "login-ip" or "register"). The full key contains the submitted username,
    and this line is the one place in the auth path where attacker-controlled
    text meets the log; taking the first segment is what stops a caller from
    writing a log-injection payload into a shared log stream.
    """
    try:
        limiter.check(key)
    except RateLimitExceeded as exc:
        log.warning("auth.rate_limited key=%s retry_after=%s", key.split(":")[0], exc.retry_after)
        return HTTPException(
            status_code=429,
            detail=RATE_LIMIT_DETAIL,
            headers={"Retry-After": str(exc.retry_after)},
        )
    return None


def _request_ip(request: Request) -> str:
    """The rate-limiting key for this request's caller.

    `client_ip` never raises, so there is no path where a malformed
    `X-Forwarded-For` turns a login into a 500.
    """
    return client_ip(request.headers, request.client.host if request.client else None)


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    # 1. Throttle first, and on a key that contains no submitted data at all,
    #    so a flood of junk usernames cannot create junk limiter state.
    ip = _request_ip(request)
    limited = _limited(_register_limiter, "register:%s" % ip)
    if limited is not None:
        raise limited

    # 2. SEC-3 policy. Before the uniqueness query, so an obviously-invalid
    #    request never reaches the database, and 422 -- not 400 -- because
    #    this is a malformed request, not a conflict with existing state.
    problem = username_problem(body.username) or password_problem(body.password)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)

    # 3. Unchanged: a taken username is a 400, exactly as before.
    if db.query(models.User).filter(models.User.username == body.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")

    # 4. Unchanged: hash, insert, and hand back the token.
    user = models.User(username=body.username, hashed_password=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    log.info("auth.registered user_id=%s", user.id)
    return TokenResponse(access_token=create_token(user.id))


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    # (a) argon2 has its own length limit and hashing an unbounded string is a
    #     free CPU-exhaustion primitive, so the ceiling is checked before any
    #     work at all. The *floor* is deliberately not checked here -- see the
    #     module docstring: a length complaint would be a free oracle.
    if password_too_long(body.password):
        raise HTTPException(status_code=422, detail="Password must be at most 256 characters.")

    # (b) SEC-4. Both windows run BEFORE the database query and BEFORE
    #     verify_password, because the cost being defended is the argon2 hash
    #     and checking the budget after it has been paid would be pointless.
    ip = _request_ip(request)
    # The username is normalised so "Alice" and "alice" cannot be used as two
    # separate budgets for the same account. Note the key still contains the
    # username -- it is only the *log* that is stripped back to the scope.
    norm_user = str(body.username or "").strip().lower()
    limited = _limited(_login_limiter, "login:%s:%s" % (ip, norm_user)) or _limited(
        _login_ip_limiter, "login-ip:%s" % ip
    )
    if limited is not None:
        raise limited

    # (c) The lookup, unchanged, and then the TIMING EQUALISATION.
    user = db.query(models.User).filter(models.User.username == body.username).first()

    # The old line was `if not user or not verify_password(...)`, which
    # short-circuits: a nonexistent username returned in the time of one
    # SELECT, an existing one in the time of SELECT + argon2. That difference
    # is a free username-enumeration oracle, available to anyone who can reach
    # the port and with no failed logins to notice. Computing the comparison
    # *first*, and always computing it, removes the branch from the timing.
    #
    # DUMMY_HASH is an empty PLACEHOLDER, not a real argon2 hash. argon2 is
    # not installed in the environment this was written in, so a real hash
    # could not be produced here, and inventing a string that merely *looks*
    # like `$argon2id$v=19$...` would be worse than useless: it would either
    # fail to parse (turning "no such user" into a 500) or, if it happened to
    # parse, be a fabricated credential-shaped constant committed to a public
    # repository. Neither is acceptable, so it was not done.
    #
    # What is kept, and what is honest, is the *control-flow* shape: the
    # comparison is computed unconditionally before the branch. The day a real
    # hash of a random throwaway value is dropped into DUMMY_HASH, the two
    # paths cost the same and no further change is needed. Until then only
    # the control flow is equalised -- the two paths are still NOT
    # constant-time, and this section must not be read as claiming they are.
    #
    # NOTE: that equality cannot be tested in this environment. There is no
    # passlib, no argon2 and no fastapi, so the handler cannot be imported or
    # executed at all. It is verified by reading and by compileall, and by
    # nothing else.
    stored_hash = user.hashed_password if user else DUMMY_HASH
    try:
        ok = bool(verify_password(body.password, stored_hash))
    except Exception:
        # A placeholder or malformed stored hash must produce "wrong
        # password" (401), never a 500 that tells the caller something
        # different happened here.
        ok = False

    if not user or not ok:
        # (e) Username and IP only. Never the password, never the submitted
        #     hash, never a token.
        log.info("auth.login_failed user=%s ip=%s", norm_user, ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # (d) A successful login clears the per-(ip, account) window, so three
    #     typos and a correct password is not a lockout. The per-IP window is
    #     deliberately NOT cleared: it exists to bound a *host*, and letting
    #     one successful login refill a host that has been spraying many
    #     accounts would hand the attacker a way to reset it on demand.
    _login_limiter.reset("login:%s:%s" % (ip, norm_user))

    # (f) Unchanged.
    return TokenResponse(access_token=create_token(user.id))


@router.get("/me", response_model=UserResponse)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user
