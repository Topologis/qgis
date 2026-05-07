"""Tiny HTTP helpers used by the export task.

We deliberately stick to the Python standard library (``urllib`` /
``http.client``) instead of pulling in ``requests``: QGIS bundles its own
Python and we don't want to ship third-party dependencies inside a plugin zip.

Two helpers are exposed:

* :func:`post_json` - blocking POST with a JSON body and a JSON response.
* :func:`put_file_with_progress` - chunked PUT used for the S3 presigned upload,
  with progress callbacks and cooperative cancellation.
"""

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Tuple

from .debug import debug_log


# Connection timeouts (seconds). The POST timeout is short because the API
# only does cheap bookkeeping; the PUT timeout is longer because S3 uploads
# can stall briefly mid-stream on slow networks.
_POST_TIMEOUT_S = 30
_PUT_TIMEOUT_S = 60

# Chunk size for streaming uploads. 64 KiB strikes a balance between the
# overhead of many small writes and reporting progress smoothly enough that
# the QGIS progress bar moves visibly.
_UPLOAD_CHUNK_BYTES = 64 * 1024

_ALLOWED_URL_SCHEMES = {"http", "https"}


class Cancelled(Exception):
    """Raised from inside the upload loop when the task is cancelled.

    The export task catches this to distinguish a user-initiated cancel from
    an actual error.
    """


def _parse_http_url(url: str) -> urllib.parse.ParseResult:
    """Return a parsed URL after rejecting non-HTTP schemes."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
        raise ValueError("URL must use http or https")
    return parsed


def post_json(url: str, payload: dict) -> Tuple[int, dict]:
    """POST ``payload`` as JSON and return ``(status, parsed_body)``.

    Network failures are returned as ``(0, {"error": ...})`` so callers can
    handle them uniformly with HTTP error responses instead of having to wrap
    every call in another try/except.
    """
    data = json.dumps(payload).encode("utf-8")
    safe_payload = dict(payload)
    if "token" in safe_payload:
        safe_payload["token"] = "<redacted>"
    debug_log(f"POST {url} payload={safe_payload}")

    try:
        _parse_http_url(url)
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
    except ValueError as e:
        debug_log(f"POST {url} rejected: {e}")
        return 0, {"error": str(e)}

    try:
        with urllib.request.urlopen(  # nosec B310 - URL scheme validated above.
            req,
            timeout=_POST_TIMEOUT_S,
        ) as resp:
            body_bytes = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        # 4xx/5xx still carry a JSON body that we want to surface to the user.
        body_bytes = e.read()
        status = e.code
    except urllib.error.URLError as e:
        # DNS failure, connection refused, TLS error, etc. - no response.
        debug_log(f"POST {url} network error: {e.reason}")
        return 0, {"error": f"Network error: {e.reason}"}

    try:
        body = json.loads(body_bytes.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        body = {"error": f"Invalid JSON response (HTTP {status})"}
    debug_log(f"POST {url} response HTTP {status}: {body}")
    return status, body


def put_file_with_progress(
    url: str,
    file_path: str,
    progress_cb: Callable[[int, int], None],
    cancel_cb: Callable[[], bool],
) -> None:
    """Stream ``file_path`` to ``url`` via HTTP PUT.

    ``progress_cb(sent, total)`` is invoked after every chunk so the UI can
    update the progress bar. ``cancel_cb()`` is polled before each chunk; if
    it returns ``True`` we raise :class:`Cancelled` and abandon the upload
    (the connection is closed in the ``finally`` block).

    Raises:
        Cancelled: The task was cancelled mid-upload.
        RuntimeError: The remote returned a non-2xx status.
    """
    parsed = _parse_http_url(url)
    debug_log(f"PUT {parsed.scheme}://{parsed.netloc}{parsed.path} from {file_path}")
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(parsed.netloc, timeout=_PUT_TIMEOUT_S)
    else:
        conn = http.client.HTTPConnection(parsed.netloc, timeout=_PUT_TIMEOUT_S)

    # Reassemble the path + query string the way ``http.client`` expects.
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    size = os.path.getsize(file_path)

    try:
        # We use ``putrequest`` / ``putheader`` / ``endheaders`` (the lower
        # level API) so we can stream the body in chunks rather than building
        # the whole request in memory.
        conn.putrequest("PUT", path, skip_accept_encoding=True)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()

        sent = 0
        with open(file_path, "rb") as f:
            while True:
                # Cooperative cancellation: check before every read so we don't
                # waste bandwidth or time after the user clicks Cancel.
                if cancel_cb():
                    raise Cancelled()
                chunk = f.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                progress_cb(sent, size)

        resp = conn.getresponse()
        status = resp.status
        body = resp.read().decode("utf-8", errors="replace")
        if not 200 <= status < 300:
            # Truncate the body so a verbose XML error from S3 doesn't blow up
            # the QGIS message log.
            raise RuntimeError(f"S3 upload failed: HTTP {status} {body[:200]}")
    finally:
        conn.close()
