"""Official Python client for EnrichGrid, the LinkedIn profile & company scraper API.

    from enrichgrid import Client
    eg = Client()                                                # reads ENRICHGRID_KEY
    lookup = eg.person("https://www.linkedin.com/in/some-profile")
    if lookup["found"]:
        print(lookup["profile"]["current_title"], lookup["profile"]["current_company_name"])

Slow lookups answer 202 "pending"; the client polls GET /v1/lookups/{id} for you (free) and retries 429/503 using
Retry-After, so every method returns a finished result. Docs: https://enrichgrid.com/api-docs
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional

import requests

__all__ = ["Client", "EnrichGridError", "AuthError", "InsufficientCredits", "InvalidRequest", "Unavailable"]
__version__ = "0.1.0"

DEFAULT_BASE_URL = "https://enrichgrid.com"
RETRY_STATUS = {429, 503}


class EnrichGridError(Exception):
    """Base error. `status` is the HTTP status (0 for network errors), `code` the API error code."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{status} {code}: {message}".strip())
        self.status, self.code, self.message = status, code, message


class AuthError(EnrichGridError):
    """401/403: missing, invalid or revoked API key."""


class InsufficientCredits(EnrichGridError):
    """402: add credits at https://enrichgrid.com/dashboard/billing."""


class InvalidRequest(EnrichGridError):
    """400/404/422: the identifier or filters can't be used."""


class Unavailable(EnrichGridError):
    """429/503 after all retries, or a network error. Nothing was charged."""


def _error_for(status: int, body: Any) -> EnrichGridError:
    detail = body.get("detail") if isinstance(body, dict) else None
    detail = detail if isinstance(detail, dict) else {"message": str(detail or "")}
    code, message = str(detail.get("error", "error")), str(detail.get("message", ""))
    cls = (AuthError if status in (401, 403) else InsufficientCredits if status == 402
           else Unavailable if status in RETRY_STATUS else InvalidRequest)
    return cls(status, code, message)


class Client:
    """Synchronous client. Thread-safe enough for `enrich_many` (one requests.Session per worker)."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 120,
                 max_wait: float = 300, max_retries: int = 5, poll_interval: float = 3,
                 session_factory: Optional[Callable[[], requests.Session]] = None):
        self.api_key = api_key or os.environ.get("ENRICHGRID_KEY", "")
        if not self.api_key:
            raise ValueError("Pass api_key or set ENRICHGRID_KEY (get one at https://enrichgrid.com/dashboard/keys).")
        self.base_url = base_url.rstrip("/")
        self.timeout, self.max_wait, self.max_retries, self.poll_interval = timeout, max_wait, max_retries, poll_interval
        self._factory = session_factory or requests.Session
        self._local = threading.local()  # one HTTP session per thread (enrich_many runs workers in threads)

    # ------------------------------------------------------------------ transport
    def _http(self) -> requests.Session:
        if getattr(self._local, "session", None) is None:
            self._local.session = self._factory()
        return self._local.session

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        headers = {"x-api-key": self.api_key, "user-agent": f"enrichgrid-python/{__version__}"}
        last: Optional[EnrichGridError] = None
        for attempt in range(self.max_retries):
            try:
                r = self._http().request(method, self.base_url + path, headers=headers, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last = Unavailable(0, "network_error", str(exc))
                time.sleep(min(2 ** attempt, 30))
                continue
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code < 400:
                return body
            err = _error_for(r.status_code, body)
            if r.status_code not in RETRY_STATUS:
                raise err
            wait = float(r.headers.get("Retry-After") or 2 ** attempt)
            if wait > 60:  # a long pause (e.g. provider maintenance): tell the caller now instead of blocking
                raise err
            last = err
            time.sleep(wait)
        raise last or Unavailable(0, "unavailable", "Retries exhausted")

    def _finish(self, data: Dict[str, Any]) -> Dict[str, Any]:
        deadline = time.time() + self.max_wait
        while data.get("status") == "pending" and time.time() < deadline:
            time.sleep(self.poll_interval)
            data = self._request("GET", f"/v1/lookups/{data['id']}")
        return data

    # ------------------------------------------------------------------ endpoints
    def person(self, identifier: str, live: bool = False) -> Dict[str, Any]:
        """LinkedIn profile URL, slug or member ID → finished lookup: {"found", "profile", "credits_charged", …}.
        1 credit if found; not found is free. live=True skips the 30-day copy and collects it again."""
        return self._finish(self._request("POST", "/v1/enrich/person", json={"identifier": identifier, "live": live}))

    def company(self, identifier: str, live: bool = False) -> Dict[str, Any]:
        """LinkedIn company URL, slug or numeric ID → finished lookup with "company" (firmographics)."""
        return self._finish(self._request("POST", "/v1/enrich/company", json={"identifier": identifier, "live": live}))

    def school(self, identifier: str, live: bool = False) -> Dict[str, Any]:
        """LinkedIn school URL, slug or numeric ID → finished lookup with "school"."""
        return self._finish(self._request("POST", "/v1/enrich/school", json={"identifier": identifier, "live": live}))

    def search(self, filters: Dict[str, Any], cursor: Optional[str] = None) -> Dict[str, Any]:
        """One page of people search: {"slugs": [...10], "next_cursor": str | None, "credits_charged": int}.
        1 credit per page with results. Filters: title, company, past_company, country, city, company_size, …"""
        return self._request("POST", "/v1/search", json={"filters": filters, "cursor": cursor})

    def search_all(self, filters: Dict[str, Any], max_pages: Optional[int] = None) -> Iterator[str]:
        """Yield profile slugs page after page until the last page (or max_pages)."""
        cursor, pages = None, 0
        while True:
            page = self.search(filters, cursor)
            yield from page.get("slugs") or []
            pages += 1
            cursor = page.get("next_cursor")
            if not cursor or (max_pages is not None and pages >= max_pages):
                return

    def lookup(self, lookup_id: str) -> Dict[str, Any]:
        """A lookup by id (free)."""
        return self._request("GET", f"/v1/lookups/{lookup_id}")

    def lookups(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Your most recent lookups, newest first (max 100, free)."""
        return self._request("GET", "/v1/lookups", params={"limit": limit}).get("data", [])

    def me(self) -> Dict[str, Any]:
        """Credit balance, plan and requests per second (free)."""
        return self._request("GET", "/v1/me")

    # ------------------------------------------------------------------ bulk
    def enrich_many(self, identifiers: Iterable[str], kind: str = "person", workers: int = 4,
                    live: bool = False) -> List[Dict[str, Any]]:
        """Enrich many identifiers in parallel; results keep the input order. Errors become
        {"identifier", "status": "error", "error": code, "message"} so one bad row doesn't stop the batch,
        except InsufficientCredits and AuthError, which stop it."""
        items = list(identifiers)
        method = getattr(self, {"person": "person", "company": "company", "school": "school"}[kind])

        def one(ident: str) -> Dict[str, Any]:
            try:
                return method(ident, live=live)
            except (InvalidRequest, Unavailable) as exc:
                return {"identifier": ident, "status": "error", "found": False, "error": exc.code, "message": exc.message}

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return list(pool.map(one, items))
