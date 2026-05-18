"""Typed wrappers around the Topologis public API.

Keeps ``http_client`` generic. Each function here owns one endpoint, normalises
its response, and returns a plain tuple the GUI can consume without knowing
about HTTP status codes or JSON shapes.
"""

from typing import Optional, Tuple

from .config import API_URL
from .http_client import get_json


def request_anonymous_session() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Mint a one-off preview token by hitting ``/api/public/anonymous-session``.

    Returns ``(token, session, error)``. On success ``error`` is ``None``;
    on any failure ``token`` and ``session`` are ``None`` and ``error``
    carries a message suitable for inline display. ``session`` is an opaque
    string the caller is expected to append to the preview URL so the server
    can identify which anonymous bucket to read.
    """
    status, body = get_json(f"{API_URL}/api/public/anonymous-session")
    if status == 200 and isinstance(body, dict) and body.get("token"):
        session = body.get("session")
        return str(body["token"]), str(session) if session else None, None

    # Surface a server-provided message when present, otherwise fall back to
    # a generic line that still tells the user which step failed.
    err = body.get("error") if isinstance(body, dict) else None
    return None, None, err or f"Could not start anonymous session (HTTP {status})"
