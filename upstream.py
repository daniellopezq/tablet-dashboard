"""Calls to cloud APIs: no redirects (the Authorization header must not leave the configured host),
bounded response size and time, errors reduced to a category that is safe to show. Used by the
assistant, voice, calendar, health checks and the admin page. Python 3 standard library only.
"""
import json
import re
import time
import urllib.error
import urllib.request

import fallback as fb
import netsim


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the Authorization header must not leave the configured host."""

    def redirect_request(self, *args, **kwargs):
        return None


class UpstreamError(Exception):
    def __init__(self, category, http_status=None, retry_after=None, error_type=""):
        super().__init__(category)
        self.category, self.http_status = category, http_status
        self.retry_after, self.error_type = retry_after, error_type


def _clean_token(value):
    value = value if isinstance(value, str) else ""
    return value if re.match(r"^[A-Za-z0-9_.-]{1,64}$", value) else ""


def classify_http_error(exc):
    """UpstreamError for an HTTP error status (the body is read only for the error type)."""
    status = exc.code
    error_type = ""
    try:
        data = json.loads(exc.read(4096).decode("utf-8", "replace"))
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            error_type = _clean_token(err.get("type")) or _clean_token(err.get("code"))
    except Exception:
        pass
    retry_after = None
    header = exc.headers.get("retry-after") if exc.headers else None
    if header and header.strip().isdigit():
        retry_after = min(max(int(header.strip()), 1), 3600)
    if status == 401:
        return UpstreamError("auth_error", status, error_type=error_type)
    if status == 402:
        return UpstreamError("quota_exhausted", status, error_type=error_type)
    if status == 403:
        return UpstreamError("access_denied", status, error_type=error_type)
    if status == 429:
        return UpstreamError("provider_rate_limited", status, retry_after, error_type)
    if status >= 500:
        return UpstreamError("upstream_5xx", status, error_type=error_type)
    return UpstreamError("upstream_rejected", status, error_type=error_type)


def post_bounded(opener, cloud, req, timeout, limit, no_response_seconds=None):
    """Send req and return the response body (at most `limit` bytes, within `timeout` overall).

    With no_response_seconds, the request goes through `cloud` (a fallback.HeaderDeadlineOpener)
    and raises fallback.NoResponse when no headers arrive in time; that is the only fallback
    trigger. Every other failure raises UpstreamError, including any received HTTP status
    (classified by status) and any failure after the headers arrived.
    """
    deadline = time.monotonic() + timeout
    if no_response_seconds is None and cloud is not None and netsim.blocked(cloud.service):
        raise UpstreamError("network_error")  # offline test with the fallback turned off
    try:
        if no_response_seconds is None:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = cloud.open(req, min(no_response_seconds, timeout), timeout)
        with resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > limit:
                raise UpstreamError("bad_response", resp.status)
            chunks, total = [], 0
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise UpstreamError("bad_response", resp.status)
                if time.monotonic() > deadline:
                    raise UpstreamError("timeout")
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        raise classify_http_error(exc)
    except (UpstreamError, fb.NoResponse):
        raise
    except (TimeoutError, OSError) as exc:
        if isinstance(exc, TimeoutError) or "timed out" in str(exc):
            raise UpstreamError("timeout")
        raise UpstreamError("network_error")
    except Exception:
        raise UpstreamError("network_error")
    return b"".join(chunks)
