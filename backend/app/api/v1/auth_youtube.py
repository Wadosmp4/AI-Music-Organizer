"""YouTube Data API OAuth login flow (KTD4, detection path).

Lets the user (re)connect the detection-path Google account through the web
app instead of hand-placing a token file. Both routes are plain browser GETs
(Google's own redirect flow requires that) — the CSRF guard in app/main.py
only gates mutating methods, so it does not (and must not) apply here.

State-CSRF protected: the state issued at /authorize must come back
unchanged at /callback (see `pending_oauth_state` in
youtube_data_api_client.py for why an in-memory slot is sufficient for this
personal single-user tool).
"""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.integrations.youtube_data_api_client import YouTubeDataApiClient, pending_oauth_state

router = APIRouter(prefix="/auth/youtube", tags=["auth"])

# Hardcoded like the Vite dev server's own origin in main.py's CSRF allowlist
# and yt_data_api_redirect_uri's default (KTD26's pinned Docker Compose
# topology) — this is where the user's browser already is.
_FRONTEND_URL = "http://localhost:5173/"


@router.get("/authorize")
def authorize() -> RedirectResponse:
    client = YouTubeDataApiClient()
    if not client.client_id or not client.client_secret:
        return RedirectResponse(f"{_FRONTEND_URL}?youtube_connect=not_configured")
    auth_url, state = client.get_authorization_url()
    pending_oauth_state.issue(state)
    return RedirectResponse(auth_url)


@router.get("/callback")
def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error is not None:
        return RedirectResponse(f"{_FRONTEND_URL}?youtube_connect=denied")
    if not state or not pending_oauth_state.consume(state):
        return RedirectResponse(f"{_FRONTEND_URL}?youtube_connect=state_mismatch")
    if not code:
        return RedirectResponse(f"{_FRONTEND_URL}?youtube_connect=missing_code")

    try:
        YouTubeDataApiClient().exchange_code_for_token(code)
    except Exception:
        return RedirectResponse(f"{_FRONTEND_URL}?youtube_connect=failed")
    return RedirectResponse(f"{_FRONTEND_URL}?youtube_connect=success")
