import os
import secrets
import time
from collections import defaultdict, deque

import httpx
from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, HTTPException, Path, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:9000")
OIDC_CLIENT_ID = os.environ["OIDC_CLIENT_ID"]
OIDC_CLIENT_SECRET = os.environ["OIDC_CLIENT_SECRET"]    # secret for the IdP
OIDC_DISCOVERY_URL = os.environ["OIDC_DISCOVERY_URL"]
SESSION_SECRET = os.environ["SESSION_SECRET"]            # signs the short-lived login-state cookie
REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "http://localhost:8000/auth/callback")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "/")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "http://localhost:8000")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"  # true on HTTPS
SESSION_TTL = 15 * 60  # seconds
RATE_LIMIT = 30
WINDOW_SECONDS = 60

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
# Holds ONLY the temporary login state (state/nonce/PKCE) between /auth/login and /auth/callback.
# Different cookie name from our real session cookie below.
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET,
                   session_cookie="oauth_tx", max_age=600, same_site="lax",
                   https_only=COOKIE_SECURE)

oauth = OAuth()
oauth.register(
    name="idp",
    client_id=OIDC_CLIENT_ID,
    client_secret=OIDC_CLIENT_SECRET,
    server_metadata_url=OIDC_DISCOVERY_URL,
    client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
)

# Server-side sessions: session id -> {user, expires, tokens}. Tokens never go to the browser.
SESSIONS: dict[str, dict] = {}

_hits: dict[str, deque] = defaultdict(deque)


def check_rate_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests")
    q.append(now)


def require_session(request: Request) -> dict:
    sid = request.cookies.get("session")
    session = SESSIONS.get(sid) if sid else None
    if not session or session["expires"] < time.time():
        SESSIONS.pop(sid, None)
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session

def require_user(session: dict = Depends(require_session)) -> str:
    return session["user"]

@app.get("/auth/login")
async def login(request: Request):
    check_rate_limit(request)
    # Sends the browser to the IdP's login page (with state, nonce and PKCE added by authlib).
    return await oauth.idp.authorize_redirect(request, REDIRECT_URI)


@app.get("/auth/callback")
async def callback(request: Request):
    check_rate_limit(request)
    try:
        # Exchanges the one-time code for tokens, server to server, using our client secret.
        # authlib also verifies state, nonce and the ID token signature.
        token = await oauth.idp.authorize_access_token(request)
    except OAuthError:
        raise HTTPException(status_code=401, detail="Login failed")
    claims = token.get("userinfo") or {}
    user = claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "user"

    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = {
        "user": user,
        "expires": time.time() + SESSION_TTL,
        "tokens": {
            "access_token": token.get("access_token"),
            "refresh_token": token.get("refresh_token"),
        },
    }
    response = RedirectResponse(FRONTEND_URL)
    response.set_cookie("session", sid, httponly=True, secure=COOKIE_SECURE,
                        samesite="lax", max_age=SESSION_TTL, path="/")
    return response


@app.post("/auth/logout")
def logout(request: Request, response: Response):
    sid = request.cookies.get("session")
    if sid:
        SESSIONS.pop(sid, None)
    response.delete_cookie("session", path="/")
    return {"ok": True}


@app.get("/auth/me")
def me(user: str = Depends(require_user)):
    return {"user": user}


async def call_backend(path: str, access_token: str):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                f"{BACKEND_URL}{path}",
                headers={"Authorization": f"Bearer {access_token}"},  # the USER's token, not a shared key
            )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Upstream unavailable")
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Session expired, please log in again")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="Upstream error")
    return r.json()


@app.get("/api/whoami")
async def whoami(request: Request, session: dict = Depends(require_session)):
    check_rate_limit(request)
    return await call_backend("/whoami", session["tokens"]["access_token"])


@app.get("/api/items")
async def list_items(request: Request, session: dict = Depends(require_session)):
    check_rate_limit(request)
    return await call_backend("/items", session["tokens"]["access_token"])


@app.get("/api/items/{item_id}")
async def get_item(request: Request, item_id: int = Path(ge=1, le=10000),
                   session: dict = Depends(require_session)):
    check_rate_limit(request)
    return await call_backend(f"/items/{item_id}", session["tokens"]["access_token"])

app.mount(
    "/",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "frontend"), html=True),
    name="frontend",
)