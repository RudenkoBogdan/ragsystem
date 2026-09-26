from pydantic import BaseModel


class RegisterRequest(BaseModel):
    """Body of ``POST /api/auth/register``.

    Both fields are bare ``str`` with no constraint attached, and that is
    deliberate. The length rules (username 3-32 and no whitespace; password
    12-256) live in ``security.policy`` and are enforced by the handler in
    ``auth/router.py``, which turns a policy message into a ``422`` whose
    ``detail`` is the policy's own wording.

    They are *not* pydantic validators, for one reason: ``pydantic`` cannot be
    imported in the environment this was written in, so a rule expressed as a
    validator would be exactly the kind of security control with no executable
    proof behind it. ``security/policy.py`` is pure and stdlib-only, and
    ``backend/tests/test_policy.py`` runs it on a bare Python. Keeping the rule
    there and the *call* in the router is what makes it testable.
    """

    username: str
    password: str


class LoginRequest(BaseModel):
    """Body of ``POST /api/auth/login``.

    **Only the upper bound applies here** -- ``auth/router.py`` checks
    ``password_too_long`` and returns ``422`` past 256 characters, because an
    unbounded password is a free CPU-exhaustion primitive for the server's
    argon2. The 12-character floor is deliberately *not* applied on this path:
    a "too short" complaint would separate "wrong password" (401) from
    "malformed password" (422), which is a free oracle about the stored
    credential. The floor is a registration-time rule; see the SEC-3 section of
    ``SECURITY.md``.
    """

    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: int
    username: str

    class Config:
        from_attributes = True
