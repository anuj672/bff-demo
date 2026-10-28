import os

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException

ISSUER = os.environ["OIDC_ISSUER"]
AUDIENCE = os.environ["OIDC_AUDIENCE"]      # the app's client ID
JWKS_URL = os.environ["OIDC_JWKS_URL"]

app = FastAPI()
jwks_client = jwt.PyJWKClient(JWKS_URL)      # fetches and caches the IdP's public signing keys

ITEMS = {1: {"id": 1, "name": "Widget"}, 2: {"id": 2, "name": "Gadget"}}


def current_user(authorization: str | None = Header(default=None)) -> dict:
    """Validate the bearer token the BFF forwards. No shared secret: we trust the IdP's signature."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/whoami")
def whoami(claims: dict = Depends(current_user)):
    return {"sub": claims.get("sub"), "issuer": claims.get("iss")}


@app.get("/items")
def list_items(claims: dict = Depends(current_user)):
    return list(ITEMS.values())


@app.get("/items/{item_id}")
def get_item(item_id: int, claims: dict = Depends(current_user)):
    if item_id not in ITEMS:
        raise HTTPException(status_code=404, detail="Not found")
    return ITEMS[item_id]