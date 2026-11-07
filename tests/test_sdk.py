"""Run: python -m unittest discover -s tests -v   (no network: an in-process fake of the EnrichGrid API)."""
import csv
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import enrichgrid  # noqa: E402
from enrichgrid import cli  # noqa: E402

enrichgrid.time.sleep = lambda s: None  # no real waiting in tests


def profile(slug):
    return {"slug": slug, "first_name": slug.title(), "last_name": "Example", "headline": f"Head of {slug}",
            "current_title": "CTO", "current_company_name": f"{slug.title()} Labs", "seniority": "cxo",
            "location": "Berlin", "location_country": "DE", "follower_count": 900, "num_connections": 500}


class FakeAPI:
    """Fake EnrichGrid API: each slug is 'pending' on POST and done on the first poll; 'nobody' is not found,
    'bad' is a 422, 'broke' a 402; the first call of 'flaky' gets a 429; search has two pages."""

    def __init__(self):
        self.lock, self.calls, self.pending, self.flaked = threading.Lock(), [], {}, False

    def __call__(self):  # used as session_factory
        return self

    def request(self, method, url, headers=None, timeout=None, json=None, params=None):
        assert headers["x-api-key"] == "k"
        path = url.split("enrichgrid.test", 1)[1]
        with self.lock:
            self.calls.append((method, path, json))
            if path == "/v1/me":
                return Resp(200, {"email": "a@b.c", "credits": 42, "plan": "free", "requests_per_second": 2})
            if path.startswith("/v1/lookups/"):
                lid = path.rsplit("/", 1)[1]
                slug = self.pending.pop(lid)
                return Resp(200, {"id": lid, "status": "done", "found": True, "credits_charged": 1, "profile": profile(slug)})
            if path == "/v1/lookups":
                return Resp(200, {"data": [{"id": "x"}] * params["limit"]})
            if path == "/v1/search":
                pages = {None: (["alpha", "beta"], "c2"), "c2": (["gamma"], None)}
                slugs, nxt = pages[json["cursor"]]
                return Resp(200, {"slugs": slugs, "next_cursor": nxt, "credits_charged": 1})
            ident = json["identifier"].rstrip("/").split("/")[-1]
            if ident == "flaky" and not self.flaked:
                self.flaked = True
                return Resp(429, {"detail": {"error": "rate_limited", "message": "slow down"}}, {"Retry-After": "0"})
            if ident == "maintenance":
                return Resp(503, {"detail": {"error": "temporarily_unavailable", "message": "paused"}}, {"Retry-After": "3600"})
            if ident == "nobody":
                return Resp(200, {"id": "n", "status": "done", "found": False, "credits_charged": 0})
            if ident == "bad":
                return Resp(422, {"detail": {"error": "invalid_identifier", "message": "Check the URL."}})
            if ident == "broke":
                return Resp(402, {"detail": {"error": "insufficient_credits", "message": "Add credits."}})
            lid = f"l{len(self.calls)}"
            self.pending[lid] = ident
            return Resp(202, {"id": lid, "status": "pending", "found": None, "poll": f"/v1/lookups/{lid}"})


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


def client(api=None):
    api = api or FakeAPI()
    return enrichgrid.Client("k", "https://enrichgrid.test", session_factory=api), api


class ClientTests(unittest.TestCase):
    def test_person_polls_until_done(self):
        c, api = client()
        r = c.person("https://www.linkedin.com/in/alpha/")
        self.assertTrue(r["found"])
        self.assertEqual(r["profile"]["current_company_name"], "Alpha Labs")
        self.assertEqual([m for m, _, _ in api.calls], ["POST", "GET"])

    def test_not_found_and_errors(self):
        c, _ = client()
        self.assertFalse(c.person("nobody")["found"])
        with self.assertRaises(enrichgrid.InvalidRequest) as e:
            c.person("bad")
        self.assertEqual((e.exception.status, e.exception.code), (422, "invalid_identifier"))
        with self.assertRaises(enrichgrid.InsufficientCredits):
            c.person("broke")

    def test_429_retried(self):
        c, api = client()
        self.assertTrue(c.person("flaky")["found"])
        self.assertEqual(sum(1 for m, p, _ in api.calls if m == "POST"), 2)

    def test_long_retry_after_raises_at_once(self):
        c, api = client()
        with self.assertRaises(enrichgrid.Unavailable) as e:
            c.person("maintenance")
        self.assertEqual((e.exception.status, e.exception.code, len(api.calls)), (503, "temporarily_unavailable", 1))

    def test_search_all_paginates(self):
        c, _ = client()
        self.assertEqual(list(c.search_all({"title": {"any": ["cto"]}})), ["alpha", "beta", "gamma"])
        self.assertEqual(list(c.search_all({"title": {"any": ["cto"]}}, max_pages=1)), ["alpha", "beta"])

    def test_me_and_lookups(self):
        c, _ = client()
        self.assertEqual(c.me()["credits"], 42)
        self.assertEqual(len(c.lookups(limit=3)), 3)

    def test_enrich_many_keeps_order_and_isolates_errors(self):
        c, _ = client()
        idents = ["alpha", "nobody", "bad", "beta", "gamma"]
        res = c.enrich_many(idents, workers=3)
        self.assertEqual([r.get("profile", {}).get("slug") for r in res], ["alpha", None, None, "beta", "gamma"])
        self.assertEqual(res[2]["status"], "error")
        self.assertEqual(res[2]["error"], "invalid_identifier")

    def test_key_required(self):
        os.environ.pop("ENRICHGRID_KEY", None)
        with self.assertRaises(ValueError):
            enrichgrid.Client()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self._orig = enrichgrid.requests.Session
        enrichgrid.requests.Session = self.api  # the CLI builds its own Client

    def tearDown(self):
        enrichgrid.requests.Session = self._orig

    def test_csv_enrich_keeps_rows_and_columns(self):
        d = tempfile.mkdtemp()
        src, out = os.path.join(d, "leads.csv"), os.path.join(d, "out.csv")
        with open(src, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["name", "LinkedIn URL"])
            w.writerows([["A", "https://www.linkedin.com/in/alpha"], ["N", "https://www.linkedin.com/in/nobody"],
                         ["Empty", ""], ["B", "beta"]])
        rc = cli.main(["--api-key", "k", "--base-url", "https://enrichgrid.test", "csv", src, "-o", out, "--workers", "2"])
        self.assertEqual(rc, 0)
        rows = list(csv.reader(open(out, encoding="utf-8")))
        self.assertEqual(rows[0][:3], ["name", "LinkedIn URL", "eg_found"])
        self.assertEqual([r[0] for r in rows[1:]], ["A", "N", "Empty", "B"])
        self.assertEqual([r[2] for r in rows[1:]], ["true", "false", "false", "true"])
        self.assertEqual(rows[1][rows[0].index("eg_current_company_name")], "Alpha Labs")
        self.assertEqual(rows[4][rows[0].index("eg_linkedin_url")], "https://www.linkedin.com/in/beta")

    def test_person_and_me_commands(self):
        self.assertEqual(cli.main(["--api-key", "k", "--base-url", "https://enrichgrid.test", "me"]), 0)
        self.assertEqual(cli.main(["--api-key", "k", "--base-url", "https://enrichgrid.test", "person", "bad"]), 1)


if __name__ == "__main__":
    unittest.main()
