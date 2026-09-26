# Security

Security model, controls and residual risk for the RAG Research Assistant.

This file was written incrementally — each control section was filled in by the
subtask that implemented it, and no section is left as a placeholder. An empty
section would have been honest, a plausible-sounding one is not. Every "what was
verified and what was only reasoned about" claim in it is itemised in the last
section of this document, and the per-control *Residual risks* lists are the
authoritative statement of what is still open. Nothing here has been fixed by
being written down.

Scope note: this is a self-hosted, single-operator application. Several
controls below are tuned for that. "Attacker" almost always means "someone
with a self-registered account", because registration is open by design.

## Reporting a vulnerability

This repository has no security policy, no private channel and no
`SECURITY.md` contact address. If you are running your own deployment and
find a problem, you already have everything you need: the code and the logs.
For an issue that affects other users of a fork, open a normal issue describing
the reproduction and the impact, and do not include live secrets.

## Security model

### Trust boundaries

| # | Boundary | What crosses it | Control |
|---|---|---|---|
| 1 | Browser → backend | Every HTTP request. Untrusted until a JWT is verified. | JWT verification in `get_current_user`; bounded list parameters; CORS is enforced by the browser at this boundary only. |
| 2 | Backend → LLM provider | The user's `base_url` and `api_key`, plus retrieved paper text. | SEC-1: `security/netguard.py` plus one choke point in `chat/service.py::_guarded_endpoint`; `LLM_ALLOW_PRIVATE_HOSTS=false` by default. This is the one boundary where a *user* chooses the destination. |
| 3 | Backend → arXiv | An operator-supplied URL, not user-supplied. | Existing ingest validation. |
| 4 | Backend → local storage | SQLite (`users`, `papers`, `chat_sessions`, `messages`), Chroma, PDFs. | `user_id` scoping on every read and delete; per-user Chroma collections. |
| 5 | Backend → its own configuration | `.env` | SEC-2: `JWT_SECRET` is validated at boot and never logged. |

### What is and is not attacker-controlled

Attacker-controlled by anyone who can reach the HTTP API, **including a
self-registered account**:

- the `base_url` and `api_key` in every chat message;
- the chat message content, and therefore the whole RAG prompt;
- the arXiv URL pasted into the library;
- the `limit`/`offset` query parameters on list endpoints, and `order` on the
  messages endpoint (SEC-6);
- registration and login credentials — i.e. they choose the `sub` they try to
  authenticate as.

**Not** attacker-controlled, and treated as trusted: the contents of `.env`,
the operator's reverse-proxy configuration, and the contents of `data/`
(owned by the operator). If an attacker can write to `.env`, none of this
document applies.

Explicitly *not* attacker-controlled is the retrieved paper text — but note
that a user chooses *which* papers are in the library, so treat it as
semi-trusted input that ends up inside a model prompt.

## Controls implemented

### SEC-2 — JWT secret required at boot

**Problem.** `JWT_SECRET` defaulted to `change-me-in-production`, a constant
public in this repository. A deployment that forgot to set it signed every
token with it, and forging a token for any `sub` is then trivial. Nothing at
startup noticed.

**Control.** `backend/main.py` calls
`security.policy.jwt_secret_problem()` before creating the database tables and
refuses to start when the secret is a known placeholder or is shorter than 32
characters. The message names the variable and the rule; it never prints the
value. `ALLOW_INSECURE_JWT_SECRET=true` disables the check and is documented
as local-development only.

**Tests.** `backend/tests/test_policy.py::JwtSecretProblemTests` — both
shipped placeholders (the `config.py` default and the `.env.example` literal),
the 31/32-character boundary, `None`, the keyword-only escape hatch, and a
check that no reason string ever contains the submitted value.

### SEC-5 — Logging and the never-logged list

**Problem.** The backend had no logging at all, so none of the above was
diagnosable after the fact.

**Control.** `security/logging_setup.py` installs exactly one
`StreamHandler(sys.stderr)` on the `ragapp` logger, with
`propagate = False` so records are not doubled by uvicorn's root handler.
`main.py` configures it before anything else exists and uses
`get_logger("main")`; later subtasks add `get_logger(...)` calls in the
routers. `LOG_LEVEL` sets the level and an unrecognised value falls back to
`INFO` rather than failing the boot.

**Never logged, by policy:**

- `JWT_SECRET`, and every other secret in `.env`;
- passwords, password hashes, and JWTs (including truncated ones);
- user-supplied `api_key` values;
- the full text of retrieved chunks, prompts or LLM answers;
- the `sub` of a failed login paired with anything that identifies the caller.

**Tests.** `backend/tests/test_logging.py` — namespacing, idempotence
(a second call must not add a second handler), level parsing, and an AST check
that the module imports no project code, which is what lets `main.py` call it
before the settings object exists.

### CORS policy

**Problem.** The middleware was mounted with `allow_origins=["*"]`,
`allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]`. Any
page the user visited could issue authenticated requests to the API and read
the responses.

**Control.** `CORS_ALLOW_ORIGINS` is a comma-separated allow-list (at most 20
entries, parsed by `security.policy.parse_cors_origins`). `"*"` combined with
`CORS_ALLOW_CREDENTIALS=true` is rejected at boot. Methods are narrowed to
`GET`/`POST`/`DELETE`/`OPTIONS`, headers to `Authorization`/`Content-Type`,
preflights are cached 10 minutes, and an empty list installs no middleware at
all (same-origin deployments need none). The decision is logged at startup as
an origin *count*, never the origins themselves.

**CORS is a browser control.** It stops other *web pages* from reading your
API. It is not authentication and it is not a CSRF defence for non-browser
clients — see "Residual risks".

### SEC-1 — Outbound LLM requests (SSRF guard)

**Problem.** `POST /api/chat/sessions/{id}/messages` accepts a `base_url` in
its body. The server passed it through — at most rewriting `localhost` to
`host.docker.internal` when it happened to be running in Docker — and then
issued an authenticated outbound `POST` to `{base_url}/chat/completions` with
the user's own `api_key` attached. Any registered account could therefore aim
the server's HTTP client at `http://169.254.169.254/` (cloud instance
metadata), `http://localhost:6379/`, or anything else listening on the LAN.
The URL rewrite was a whole-string `re.sub`, so it also rewrote the word
`localhost` inside a *path or a query string*, which is not what a
"container cannot reach the host loopback" fix was supposed to touch. On a
non-200 the response body was interpolated into the exception message and
serialised straight into the terminal `done` event as `"error": str(exc)` —
which made the same hole a **read** oracle, not just a connect oracle.

**Control.** `security/netguard.py` (pure, stdlib-only, no project imports)
plus one choke point, `chat/service.py::_guarded_endpoint`, which every
outbound LLM request goes through:

- a character allow-list, so a space, a CR/LF, a NUL, a backslash or a
  non-ASCII homoglyph is not a URL;
- a scheme allow-list of exactly `http` and `https`, which also rejects
  `file:`, `gopher:`, and the schemeless `localhost:11434`;
- no `user:password@` in the authority, and a port restricted to `1..65535`;
- a **host-scoped** Docker rewrite (`localhost`, `127.0.0.1`, `::1` →
  `host.docker.internal`), replacing the whole-URL substitution;
- the host is resolved and **every** address it resolved to must be public
  (or, with the flag, public + RFC1918 + loopback);
- anything the guard does not understand is refused, including an unexpected
  exception inside the guard itself — it fails closed;
- `client_message()` is a fixed sentence per code, and the `done` event now
  carries `public_error_message(exc)` instead of `str(exc)`, so no provider
  body, resolved address or internal URL can reach a client.

**`LLM_ALLOW_PRIVATE_HOSTS` (default `false`).** The deliberate behaviour
change: a request-supplied `base_url` that resolves to a non-public address
is now **rejected**, where before it was used. A private-LAN or local Ollama
reached through the browser's own settings must now set
`LLM_ALLOW_PRIVATE_HOSTS=true` on the server. The flag re-permits loopback
and RFC1918 **by design**; it is not a switch that re-opens everything.
Link-local, multicast, unspecified, reserved and unparseable addresses stay
refused even with it on, which is what keeps `169.254.169.254` closed. The
refusal sent to the client names the variable verbatim so the operator does
not have to guess.

**Deliberately not user-reachable.** The settings-origin path
(`OLLAMA_BASE_URL` / `OPENROUTER_BASE_URL`) is still parsed and validated —
a typo in `.env` is worth catching — but the address policy does not apply to
it, because operator configuration is not attacker-controlled and a
self-hosted private Ollama is a legitimate deployment. No request can reach
that branch: `origin` is `"request"` if and only if a `base_url` came in the
body.

**Tests.** `backend/tests/test_netguard.py` — 72 tests, no network: every
address category, the whole of `127/8`, `::1`, `fe80::1`, `169.254.169.254`
as link-local rather than private, each rejection code, the Docker rewrite
(including the `?x=localhost` case that proves the old blind substitution is
gone), one-bad-address-poisons-the-batch, and the property that no
`client_message()` ever contains its own `reason`.

**Residual risks.**

- **DNS rebinding / TOCTOU is not closed, and is deferred.** The guard
  resolves the host, then `aiohttp` resolves it *again* when it opens the
  connection. Between the two, an attacker-controlled record can change
  answer. The proper fix is an `aiohttp.AbstractResolver` that hands the
  connector the address the guard already checked while preserving the
  original `Host` header and TLS SNI, so the checked address *is* the
  connected address. It is a real change to the connection path and it is
  **deferred**, not shipped — the current guard narrows the attack to a
  window it does not close.
- **All returned addresses are checked, not one.** A host with both a public
  and a loopback record is refused outright, even if a legitimate resolver
  returns both. This is the strict reading, chosen because picking "the
  first address" leaves the choice of address to whoever controls DNS. The
  cost is that a dual-homed or split-horizon host cannot be used from a
  request.
- **`LLM_ALLOW_PRIVATE_HOSTS=true` re-permits loopback and RFC1918.** That
  is the documented purpose, but it also re-permits reaching any service on
  the operator's own network, from any account that can register. Treat it as
  "this deployment's users are trusted with the LAN", and never set it on a
  multi-tenant or internet-facing install.
- The client still learns *that* a request was refused and *why* at the level
  of "blocked address" vs "bad scheme" — enough to probe which hosts exist.
  That is a deliberate trade for a usable error message over `169.254.169.254`
  and the flag name; the resolved address itself is never sent.

### SEC-3 — Credential length policy

**Problem.** `RegisterRequest` declared `username: str` / `password: str` with
no constraint at all. An empty password, a one-character password and a
one-character username were all accepted, and the account was then protected
by nothing but argon2 — that is, by a hash function whose entire job is to be
slow, asked to be fast in order to make a guess cheap.

**Control.** `security/policy.py` (pure, stdlib-only) is the single place the
rules live, and `auth/router.py` calls it and turns the returned message into
a `422`:

| Field | Rule | Message |
|---|---|---|
| `username` | 3–32 characters, no whitespace | `Username must be at least 3 characters.` / … |
| `password` | 12–256 characters | `Password must be at least 12 characters.` / … |

**Length is deliberately the only rule.** No composition rule ("one upper, one
digit, one symbol"): it reliably produces `Password1!`, which is in every
published cracking dictionary, while making the policy a support-ticket
generator. There is deliberately no character-class rule on usernames either —
usernames are not secrets, and a pattern that rejects a unicode letter or an
underscore is a worse product than one that accepts it.

**Deliberate behaviour change: the floor is NOT applied to login.** Only the
256-character ceiling is, on `POST /api/auth/login`. A `422` saying "password
must be at least 12 characters" on the *login* endpoint would separate "wrong
password" (401) from "malformed password" (422) — which is a free oracle about
the stored credential, and it would leak a fact about a *guess* that the 401
path otherwise refuses to confirm. The ceiling is the exception because an
unbounded password is a CPU-exhaustion primitive for the server's argon2, and
that one is a property of the request, not a claim about the stored secret.

**Why the rules are in the router and not in the pydantic schema.** The schema
fields are still bare `str`, and that is intentional. `pydantic` cannot be
imported in the environment this was written in, so a rule expressed as a
validator would be a security control with no executable proof behind it.
`security/policy.py` is pure and stdlib-only, and `test_policy.py` executes it
on a bare Python; the router is only the place that turns its message into an
HTTP status. The public contract is unchanged — same fields, same types, same
`422` FastAPI already returned for a malformed body.

**Tests.** `backend/tests/test_policy.py` — the rules themselves. The router
wiring is **verified by reading only**: `backend/auth/router.py` imports
`fastapi`, `sqlalchemy` and `passlib`, none of which is installed, so the
handler cannot be imported or executed here. `test_ratelimit.py`'s
`RouterWiringTextTests` pins it as source text, and `compileall` checks the
syntax. Nothing in this section was exercised by making a request.

**Residual risks.**

- **12 characters is a floor, not a breach-resistance guarantee.** It is the
  same length NIST and OWASP give as a *minimum for user-chosen* secrets, and
  the real defence is argon2 plus the SEC-4 throttle. `correct horse battery
  staple` passes; `aaaaaaaaaaaa` passes and is guessable in seconds offline.
- **No breach-corpus check is performed.** Checking a candidate against a
  known-leaked-password list is the single most effective thing to do with a
  submitted password, and it is **not** implemented: there is no such corpus
  in this repository and adding one means adding a third-party package, which
  this work does not do. Until it exists, the policy length-checks and nothing
  more.
- **The rules are not enforced on the stored credential.** SQLite already holds
  whatever hashes exist; a password registered before this change is not
  retro-fitted, and there is no expiry or forced-rotation mechanism. A
  deployment that wants one has to add it.
- **Whitespace-only passwords under 12 characters are rejected, but 12 spaces
  are accepted.** That is intentional and `test_policy.py` asserts it
  deliberately, but it does mean a user can register a password with no
  entropy in it and get no warning.

### SEC-4 — Authentication rate limiting

**Problem.** `login()` ran `verify_password()` on every unauthenticated
request. argon2 is deliberately expensive — that is what makes a stolen hash
expensive to crack — and the endpoint was open, unthrottled and unlogged. That
made it two things at once: a **free CPU-exhaustion primitive** (a single
request cost the server hundreds of milliseconds of CPU, which the attacker
paid nothing for) and an **online password-guessing oracle** with no ceiling
on attempts. `register()` had the same shape: five accounts a minute is five
free, immediately usable, unthrottled accounts.

**Control.** `security/ratelimit.py` (pure, stdlib-only, no project imports)
provides an exact sliding-window limiter, and `auth/router.py` builds three
module-level limiters from the `AUTH_*` settings:

| Env var | Default | Window | Key |
|---|---|---|---|
| `AUTH_LOGIN_RATE_LIMIT` | `10` | `AUTH_LOGIN_RATE_WINDOW` = `60` s | `login:<ip>:<username>` |
| `AUTH_LOGIN_IP_RATE_LIMIT` | `30` | `AUTH_LOGIN_IP_RATE_WINDOW` = `60` s | `login-ip:<ip>` |
| `AUTH_REGISTER_RATE_LIMIT` | `5` | `AUTH_REGISTER_RATE_WINDOW` = `300` s | `register:<ip>` |

- **Three windows, not one.** Per-account bounds guessing against *one*
  account; per-IP bounds a host spraying *many* accounts, which the per-account
  window cannot see because each new name is a new bucket; per-IP registration
  bounds automated account creation.
- **The limiters run before the database and before argon2.** That ordering is
  the entire finding — checking the budget after the expensive call would
  leave the cost exactly where it was and only slow the rejection down.
- **A rejected attempt is not recorded.** Otherwise a client could hold a
  lockout open by continuing to try, turning a throttle into a self-inflicted
  DoS against whoever shares the key.
- **A successful login clears the per-(ip, account) window** so three typos
  and a correct password is not a lockout. The per-IP window is **not** cleared
  on success: it exists to bound a host, and refilling it on demand would hand
  an attacker a reset button.
- **The username is lower-cased and stripped** before it becomes a key, so
  `Alice`, `alice` and `ALICE` cannot each hold a separate budget.
- **The clock is `time.monotonic`, not `time.time`.** An NTP step forwards can
  extend a window and one backwards can collapse it; a monotonic clock cannot
  be moved by anything outside the process. It is injectable for tests.

**New response: `429` with `Retry-After`.** Body
`{"detail": "Too many attempts. Try again later."}`, header `Retry-After: <n>`
in whole seconds, always `>= 1` (a `Retry-After: 0` is an instruction to
retry immediately, which would advertise that the limit is not real). The
frontend already surfaces a non-2xx `detail` verbatim
(`frontend/src/lib/api.ts`), so the message reaches the login form with no
frontend change.

**Logging.** A refusal logs at `WARNING`:
`auth.rate_limited key=login retry_after=42`. The key is **stripped to its
scope** (`key.split(":")[0]`) — the full key contains the submitted username,
and this is the one place in the auth path where attacker-controlled text
meets the log. Failures log `auth.login_failed user=<name> ip=<ip>` at `INFO`;
never the password, never a hash, never a token.

**Tests.** `backend/tests/test_ratelimit.py` — the limiter and `client_ip`,
executed: the limit boundary, the non-extending rejection, clock-driven
expiry, the `retry_after` floor at the exact-window boundary, reset/peek
semantics, key independence, the memory ceiling, FIFO-not-LRU eviction, every
`client_ip` precedence rule including the empty-header and log-injection cases,
and an AST check that the module imports no third-party package and cannot
open a socket. **No network is touched and no real clock is read** — every test
passes an explicit `FakeClock`. The router wiring is text-asserted only, for
the reason given in SEC-3 above.

**Residual risks.**

- **⚠️ OPEN — the username-enumeration timing oracle is only half closed.**
  `DUMMY_HASH` in `backend/auth/router.py` is currently a labelled empty-string
  **placeholder**, not a real precomputed argon2 hash. argon2 could not be
  installed or run in the environment this was written in, so a genuine hash of a
  throwaway value could not be produced here, and a string that merely *looked*
  like `$argon2id$v=19$…` would either fail to parse — turning "no such user"
  into a 500 — or be a fabricated credential-shaped constant committed to a
  public repository. Neither is acceptable, so neither was done.
  **What did land:** the *control-flow* shape. `verify_password` is now computed
  unconditionally, before the `if not user` branch, so both paths go through
  the same call. **What did not:** the two paths are therefore **not**
  constant-time today — the user-not-found path fails to parse the placeholder
  immediately while the user-found path pays a full argon2 verify, so the timing
  difference the old `if not user or not verify_password(…)` had is narrowed,
  not removed. The `except Exception: ok = False` around the call is deliberate
  and stays: an unparseable stored hash must produce a `401`, never a `500` that
  tells the caller something different happened.
  **The fix, written down so nobody has to rediscover it:** generate a real
  argon2 hash of a random throwaway value — at import time, or as a
  precomputed constant committed alongside the code, since the salt is not
  secret by construction — and drop it into `DUMMY_HASH`. `verify_password`
  against it then costs the same full argon2 work as a real check, the
  comparison is `False` for any real submitted password, and no code path
  changes. Verify it with a test that asserts `DUMMY_HASH` parses as an argon2
  hash and that `verify_password("anything", DUMMY_HASH)` is `False`; neither is
  executable here, because there is no argon2 and no `passlib` in this
  environment.
- **The limiter is in-process, so `--workers N` multiplies every limit.**
  `10` login attempts per 60 s becomes `10 × N` across `N` uvicorn workers,
  and a rolling restart resets the counters entirely. This is the single most
  important thing to know about the control: it is per-process, not
  deployment-wide. A shared backend (Redis, a database table) is the fix and is
  **not** implemented, because it means a new dependency.
- **`X-Forwarded-For` is attacker-controlled unless a trusted reverse proxy
  overwrites it. This is a DEPLOYMENT REQUIREMENT, not a code fix.** The
  limiter reads the left-most entry, which is the original client *only* if
  the proxy replaces the header instead of appending to it. A proxy that
  passes a client-supplied `X-Forwarded-For` through lets one attacker mint a
  fresh per-IP budget per request and the per-IP window stops meaning anything;
  the per-account window still applies. See "A trusted reverse proxy must
  overwrite X-Forwarded-For" under *Deployment requirements* for how to
  verify it.
- **Requests with no derivable IP share one `"unknown"` bucket.** If a
  deployment strips the forwarded headers and the client address is missing,
  every anonymous caller in the deployment lands in the same per-IP bucket, so
  one attacker can throttle everyone else. This is the fail-closed direction —
  but it is a real availability risk, not a rounding error.
- **The limiter bounds the ONLINE oracle only.** It does nothing about an
  offline attack on a stolen `users` table: once the hashes are out, the
  attacker cracks at their own speed with their own CPU. The controls for that
  are argon2's cost parameters and the password floor in SEC-3, not this.
- **Memory is bounded at 10 000 keys per limiter, and eviction is
  FIFO-by-insertion.** The bound is mandatory — an unauthenticated endpoint
  otherwise becomes a key-space-exhaustion vector — but eviction means an
  attacker who floods one-shot `X-Forwarded-For` values can *evict* a hot
  key, including the one he is being throttled on, and resume guessing against
  that account with a fresh budget. LRU would be worse (it is trivially
  steered), and neither is fixed; the honest statement is that the ceiling
  trades unbounded memory for a bounded bypass.
- **No lockout notification and no admin reset.** A user who trips the limit
  waits out the window; nothing tells them why, and there is no support path
  to clear it early.

### SEC-6 — Bounded list endpoints

**Problem.** `GET /api/papers`, `GET /api/chat/sessions` and
`GET /api/chat/sessions/{id}/messages` all ended in `.all()`. A library of a
few thousand papers, or a session of a few thousand messages, came back in
one response, and the row order was whatever the database felt like — which
also made offset paging impossible to do correctly even if it were
available.

**Control.** All three take `limit` (default **200**, max **200**) and
`offset` (default 0, min 0), validated by `security.policy.resolve_page`
before any query is built, and each orders its rows by a total key before
bounding them:

| Endpoint | Order | Notes |
|---|---|---|
| `GET /api/papers` | `Paper.id` | The response is still a bare JSON array. |
| `GET /api/chat/sessions` | `updated_at DESC, id DESC` | `id` breaks `updated_at` ties so a page is a stable set. |
| `GET /api/chat/sessions/{id}/messages` | `created_at, id` | Unchanged from before, and still the tiebreak the old code relied on. |

- **The default is the cap.** Asking for nothing gets you 200 rows; there is
  no value of "no limit" that means the whole table, and `limit=0` is a 422
  rather than an empty response. A limit above 200 is a **422** from
  FastAPI's `ge`/`le` bounds (`resolve_page` is the in-process policy and
  would refuse the same values; the `except PolicyError` in each handler is
  the belt-and-braces case where a handler's own default and the policy ever
  disagree).
- **`order` is new on messages only**, defaulting to `"asc"` and constrained
  to `^(asc|desc)$`. `order=desc` selects the **newest** `limit` rows and then
  re-sorts them ascending before serialising — the standard last-page idiom,
  and the only way to ask for the newest N at all, since `LIMIT/OFFSET` count
  from the start of the ordering. It is what lets a client page a long
  conversation from the end without loading it whole.
- Nothing else about the endpoints changed: same paths, same
  `response_model`, same array shape, same `SessionResponse` /
  `PaperResponse` / `MessageResponse` fields.

**Tests.** `backend/tests/test_pagination.py` — `resolve_page` executed
directly (in range, zero, over the cap, negative offset, absent limit), the
"default is the cap" property, the last-page idiom as a pure list operation,
and text/AST assertions that the three handlers no longer call `.all()`,
do mention `limit`/`offset`/`order`, and that the route decorators and
response models are unchanged. See the honesty note in that module's
docstring: the routers cannot be imported in the environment this was
written in, so the wiring is pinned by reading the source, not by executing
the handler.

**Residual risks.**

- **Paging is O(offset).** `LIMIT/OFFSET` makes the database count and
  discard the skipped rows, so a deep page into a large library is
  progressively slower and a concurrent insert or delete can shift rows
  between two pages. Keyset pagination (`WHERE id > :last_seen ORDER BY id`)
  is the fix and is **not** implemented — it changes the response contract
  for the client, which is out of scope here.
- **The bound is per response, not per account.** 200 rows per request still
  allows a client to loop, so this limits the blast radius of one request
  rather than the total work an account can cause. Rate limiting, not
  pagination, is the control for that.
- Nothing enforces a maximum library or session size; a user can still grow
  one, and only ever see it 200 rows at a time.

## Configuration

### Environment variables added by this work

| Variable | Default | Risk it removes |
|---|---|---|
| `ALLOW_INSECURE_JWT_SECRET` | `false` | The escape hatch from SEC-2. Local dev only. |
| `LLM_ALLOW_PRIVATE_HOSTS` | `false` | The authenticated-SSRF read-oracle (SEC-1). |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Wildcard-origin credentialed reads from any web page. |
| `CORS_ALLOW_CREDENTIALS` | `false` | The "wildcard + credentials" combination browsers reject and proxies may honour. |
| `LOG_LEVEL` | `INFO` | Nothing exploitable; it is what makes the other controls diagnosable. |
| `AUTH_LOGIN_RATE_LIMIT` / `AUTH_LOGIN_RATE_WINDOW` | `10` / `60` | Unthrottled argon2 CPU-burn and offline-cracking oracle (SEC-4). |
| `AUTH_LOGIN_IP_RATE_LIMIT` / `AUTH_LOGIN_IP_RATE_WINDOW` | `30` / `60` | One host spraying many accounts (SEC-4). |
| `AUTH_REGISTER_RATE_LIMIT` / `AUTH_REGISTER_RATE_WINDOW` | `5` / `300` | Automated account creation (SEC-4). |

No existing variable changed, and no existing value was altered. Every new
default is the safe one: an unset deployment is the *stricter* one.

All 13 variables above are present in `.env.example`, each with a comment
saying what it does and, where it matters, that it must not be set in a deployed
environment. The diff to that file is **purely additive**: every one of the seven
pre-existing names — `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `LLM_PROVIDER`,
`OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `JWT_SECRET`, `BACKEND_DOCKERFILE` — is
byte-identical to its shipped value, including the `JWT_SECRET` placeholder
itself, which stays recognisable as a placeholder on purpose. The new
`# --- Security ---` block is appended below the last pre-existing line.

### Recommended production values

```bash
# A real secret, generated per deployment, never committed.
JWT_SECRET=<python3 -c "import secrets;print(secrets.token_urlsafe(48))">

ALLOW_INSECURE_JWT_SECRET=false      # must be false in production
CORS_ALLOW_ORIGINS=https://your-frontend.example.com
CORS_ALLOW_CREDENTIALS=false        # true only if you actually need cookies cross-origin
LLM_ALLOW_PRIVATE_HOSTS=false        # true only for a deliberate local Ollama
LOG_LEVEL=INFO                       # DEBUG while diagnosing
```

Rotate `JWT_SECRET` by changing it and restarting; every issued token becomes
invalid immediately, so treat it as a session reset.

## Deployment requirements

### A trusted reverse proxy must overwrite X-Forwarded-For

`SEC-4`'s per-IP throttles identify a caller by its source address. Behind a
proxy, that is the proxy unless the proxy **overwrites** the header — never
appends to a client-supplied one. A proxy that passes `X-Forwarded-For`
through lets one attacker rotate the value at will and the IP throttle stops
meaning anything.

Verify from outside:

```bash
curl -s -H 'X-Forwarded-For: 1.2.3.4' https://your-api/api/health -o /dev/null -w '%{http_code}\n'
# then check the backend log: the address attributed to that request must be
# the proxy's, not 1.2.3.4.
```

With no proxy in front, the client address is the real one and nothing is
needed.

### CORS origins for a non-default deployment

If you serve the frontend from a different origin than `http://localhost:3000`,
add that exact origin — scheme, host and port — to `CORS_ALLOW_ORIGINS`.
Requests from an origin you did not list get no CORS headers and the browser
blocks the response. That is the correct, visible failure; do not "fix" it by
setting `*`.

## Residual risks and known limitations

### Length is a floor, not a proof of credential strength

The password policy is a minimum length and a maximum length. It rejects the
empty string and the one-character passwords the register endpoint used to
accept, and nothing else. Twelve spaces is a valid password here, and that is
asserted in the test suite on purpose: a composition rule produces
`Password1!`, which is in every cracking dictionary ever published, while a
12+ character passphrase is not. The real control is argon2 plus the login
throttle; treat the length floor as the removal of a trivially guessable
class, not as credential strength.

The same honesty applies to `JWT_SECRET`: "not a placeholder and at least 32
characters" admits `aaaa…a`. Generate it; do not type it.

### CORS is browser-enforced only

CORS is a browser mechanism. It stops a malicious *web page* from reading your
API. It does nothing against a non-browser client that already has a token, and
it is not a CSRF defence: the API is authenticated with a bearer token the
browser attaches from `localStorage`, not with an ambient cookie, so a
cross-site form post carries no credentials — but if you set
`CORS_ALLOW_CREDENTIALS=true` and start relying on cookies, you have changed
that assumption and need a real CSRF defence.

### ⚠️ OPEN — username enumeration by timing is narrowed, not closed

The full statement is under *SEC-4 → Residual risks*. The short form: the
control-flow shape in `login()` is right (the password comparison is computed
before the branch, not short-circuited by it), but `DUMMY_HASH` is still an
empty-string placeholder rather than a real argon2 hash of a throwaway value,
because argon2 could not be run in the environment this was written in. The
user-not-found path therefore returns faster than the user-found path, and the
difference is still measurable. **This is not fixed.** The fix is one
precomputed constant.

### What this suite does not cover

The suite is stdlib-only by construction — `ModuleHygieneTests` fails the build
if any file in `backend/tests/` or any pure module imports a third-party
package — and everything below is outside what that shape can reach.

- **No HTTP, and no SSE wire.** No request was ever sent, no response parsed,
  no status code observed, no CORS preflight seen, no SSE frame read off a
  socket. Every `429`, `422`, `401` and `404` described in this document is
  what the code says it returns.
- **No database.** No SQLite file was opened and no SQL was executed. The
  `limit`/`offset`/`order` wiring, the `.all()` removals, the tie-breaking
  `order_by` and the registration uniqueness check are all reviewed by reading
  and by AST/text assertions, never by running a query.
- **No Chroma, no embeddings, no retrieval.** No vector store was created, no
  model loaded, no top-k search performed.
- **No argon2 and no password hashing.** `passlib` and `argon2` are absent, so
  no hash was ever created, compared or timed. Everything about SEC-3's and
  SEC-4's credential handling — including the timing equalisation — is
  unexercised.
- **No DNS and no network of any kind.** `security/netguard.py`'s address
  classification is executed on literal address strings, and
  `resolve_addresses` is always given an injected fake resolver. No `getaddrinfo`
  was called, and `chat/service.py::_guarded_endpoint`'s `run_in_executor` call
  was never executed.
- **No frontend compile, typecheck, lint, build or render.**
  `frontend/node_modules` does not exist and `node`, `npm`, `npx` and `tsc` are
  not on `PATH`, so no frontend command can run at all. The frontend guard is a
  text grep over `frontend/src`, and it is a smoke alarm rather than a proof.
- **The router-wiring assertions are AST and text checks, not imports.**
  `backend/main.py`, `backend/config.py`, `backend/auth/router.py`,
  `backend/chat/router.py`, `backend/chat/service.py` and
  `backend/papers/router.py` cannot be imported — `fastapi`, `pydantic`,
  `pydantic_settings`, `sqlalchemy`, `aiohttp`, `jose` and `passlib` are all
  absent — so every claim that they *call* a tested function correctly is
  established by reading them, and by `compileall` proving they parse.
- **The `.env.example` and `README.md` edits are prose.** Nothing validates
  them.

## Non-security findings

### SEC-7 — Accessibility (out of scope here)

The application has effectively no accessibility: no `aria-*` and no `role`
almost everywhere, mouse-only controls, and one keyboard handler in the whole
app. This is a real defect and it is not a security defect, so it is out of
scope for this series. It needs its own pass.

### Frontend has no raw-HTML rendering path (preserved, with a regression test)

The audit cleared the frontend of any raw-HTML path and marked that a hard
property: React escapes by default, and that default is the XSS control for
model-generated markdown and KaTeX output. `dangerouslySetInnerHTML` does not
appear anywhere in `frontend/src`, and `components/MathContent.tsx` uses only
`remarkGfm`, `remarkMath` and `rehypeKatex` — adding `rehype-raw` would turn
escaping off inside an otherwise-escaping renderer.

`backend/tests/test_frontend_hardening.py` is a text-level regression guard for
both spellings. It is a smoke alarm, not a proof; see its module docstring.

## What was verified and what was only reasoned about

**One reason explains every "only reasoned about" row below, so it is stated
once.** `fastapi`, `pydantic`, `pydantic_settings`, `sqlalchemy`, `aiohttp`,
`jose`, `passlib` and `chromadb` are **not installed** in the environment this
was written in; `frontend/node_modules` **does not exist**; and `node`, `npm`,
`npx` and `tsc` are **not on `PATH`**. Consequently **the application was never
started, no HTTP request was made, no SSE frame was read, no DNS lookup was
performed and no argon2 call was ever made**, and `python3 -m compileall` proves
**syntax only** — it cannot catch a missing dependency, a bad import, a type
error or any behavioural regression. The gap is not "the tests were a bit
weak"; it is that most of the changed code is wiring, and wiring needs a
runtime.

### Executed — commands actually run, with their real result

| Claim | How it was executed | Result |
|---|---|---|
| `security/policy.py` (SEC-2, SEC-3, CORS, SEC-6) | `test_policy.py`, 70 cases, imported directly | pass |
| `security/logging_setup.py` (SEC-5) | `test_logging.py`, 24 cases, imported directly | pass |
| `security/netguard.py` (SEC-1 pure half) | `test_netguard.py`, 72 cases, fake resolver, **no network** | pass |
| `security/ratelimit.py` (SEC-4) | `test_ratelimit.py`, 87 cases, explicit `FakeClock`, **no real clock** | pass |
| Pure modules stay stdlib-only | `ModuleHygieneTests` AST checks over `citations.py`, `coverage.py`, `policy.py`, `logging_setup.py`, `netguard.py`, `ratelimit.py` and every file in `backend/tests/` | pass |
| Frontend has no raw-HTML path | `test_frontend_hardening.py`, 11 text assertions over `frontend/src` — a grep, **not a build** | pass |
| Pre-existing behaviour (citations, coverage, prompt mapping, abstention) | `test_citations.py` 98, `test_coverage.py` 51, `test_prompt_mapping.py` 11, `test_abstention.py` 9 | pass |
| Pagination policy + the last-page idiom as a pure list operation | `test_pagination.py`, 29 cases (`resolve_page` executed; the `order=desc` reversal executed as list logic) | pass |
| Whole suite | `python3 -m unittest discover -s backend/tests -t backend -v` | **462 tests, `OK`, 0 skipped** |
| Every backend module parses | `python3 -m compileall -q backend` | exit 0 — **syntax only** |
| Still on the chartered branch, nothing committed | `git branch --show-current`, `git status --porcelain`, `git diff --stat` | branch correct, uncommitted tree |
| No secret in the diff or the new files | `grep -inE` over `git diff` and over `backend/security`, `backend/tests`, `SECURITY.md` | empty |

A `skipped=` suffix on that summary line would **not** be a pass: the AST
loaders in `test_prompt_mapping.py` and `test_abstention.py` degrade to a skip
if `build_system_prompt` or `stream_rag_response` is renamed, and a skip reads
exactly like a pass in CI output. The line above is a bare `OK` with no
`(skipped=…)` suffix, and both loaders were additionally run on their own to
confirm they execute rather than skip.

### Reasoned about — reviewed in the diff, never run

| Claim | What could not be checked |
|---|---|
| `main.py`: both `SystemExit` boot checks, and the boot check really runs before `Base.metadata.create_all` | `compileall` proved it parses; it proved nothing about the two `SystemExit` paths, the `CORSMiddleware` keyword arguments, or the import/ordering. The *policy functions* they call are tested; the call sites are read. |
| `main.py`: no `allow_origins=["*"]` on the mounted middleware | A grep and a read. Whether the browser actually refuses the preflight was never observed. |
| `config.py`: all 13 new `Settings` fields | Ordinary pydantic-settings fields with literal defaults. A typo in a field name would be a **silent behaviour change, not a crash**, and would not have been caught here. |
| `chat/service.py`: `_guarded_endpoint` as the single choke point, including the `loop.run_in_executor` call around `resolve_addresses` | The pure decision logic is tested; the `async` wrapper, the executor hop and its `functools.partial` were never executed. Whether a real `getaddrinfo` is ever reached from a request is unproven. |
| `chat/service.py`: the upstream non-200 now raises `UpstreamProviderError` instead of `RuntimeError(url, body)` | `str(exc)` of the new class is asserted in isolation; that the non-200 path actually raises it, and that `redact_url`/`redact_body` see the real values, was never run. |
| `chat/router.py`: the SSE `done.error` value is `public_error_message(exc)`, not `str(exc)` | The whole generator, its `except` block and the `data: {...}` framing were never run. No SSE frame was ever produced or parsed. |
| `chat/router.py` and `papers/router.py`: `limit`/`offset`/`order` wiring, `order_by`, the `desc` last-page idiom | Pinned by AST/text assertions and by executing `resolve_page` and the reversal as pure list operations. No SQL was executed and no response was serialised. |
| `papers/router.py`: a fixed 500 `detail` on ingest failure, and the `log.exception` that replaces the leak | Read. No arXiv call, no PDF, no PyMuPDF, no vector store. |
| **every line** of `auth/router.py`: the three limiters, the 429 + `Retry-After`, the `Request` injection, the `password_too_long` 422, the username/policy 422, and the timing-equalisation shape | Read, plus `compileall`. The handler cannot be imported or executed at all: no `fastapi`, no `sqlalchemy`, no `passlib`, no `jose`, no HTTP. **The timing equalisation is additionally unverifiable even in principle here** — proving it would require running argon2, and its correctness today is limited by the placeholder `DUMMY_HASH` (see *Residual risks*). |
| `.env.example` and `README.md` | Prose. Nothing validates them, and nothing checks that a documented default matches `config.py`. |
| Everything in `frontend/`, including the `getMessages` last-page call | No build, no typecheck, no lint, no render. It is a one-line URL query string against a documented endpoint, but it was never executed and never type-checked. |
