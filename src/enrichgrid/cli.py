"""`enrichgrid` command line: look up profiles and companies, search people, enrich a CSV file.

    export ENRICHGRID_KEY=...
    enrichgrid person https://www.linkedin.com/in/some-profile
    enrichgrid company https://www.linkedin.com/company/some-company
    enrichgrid search '{"title": {"any": ["cto"]}, "country": ["DE"]}' --pages 2
    enrichgrid csv leads.csv -o leads-enriched.csv
    enrichgrid me
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import List, Optional

from . import Client, EnrichGridError, __version__

CSV_FIELDS = ["found", "first_name", "last_name", "headline", "current_title", "current_company_name", "seniority",
              "location", "location_country", "follower_count", "num_connections", "linkedin_url"]


def _print(data) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _find_column(header: List[str], rows: List[List[str]], wanted: Optional[str]) -> int:
    if wanted:
        if wanted not in header:
            raise SystemExit(f"Column {wanted!r} not found. Columns: {', '.join(header)}")
        return header.index(wanted)
    for i, name in enumerate(header):
        if any(k in name.lower() for k in ("linkedin", "profile", "url", "slug")):
            return i
    for i in range(len(header)):
        if any("linkedin.com/in/" in (r[i] if i < len(r) else "") for r in rows[:50]):
            return i
    raise SystemExit("Couldn't find the LinkedIn URL column; pass --column NAME.")


def cmd_csv(client: Client, args) -> int:
    with open(args.input, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
        rows = list(csv.reader(f, dialect))
    if not rows:
        raise SystemExit("The file is empty.")
    header, data = rows[0], rows[1:]
    col = _find_column(header, data, args.column)
    idents = [(r[col] if col < len(r) else "").strip() for r in data]
    todo = [i for i in idents if i]
    print(f"Enriching {len(todo)} rows from column {header[col]!r} with {args.workers} workers…", file=sys.stderr)
    results = iter(client.enrich_many(todo, workers=args.workers, live=args.live))
    out_rows, found = [], 0
    for r, ident in zip(data, idents):
        res = next(results) if ident else {"found": False, "status": "skipped"}
        p = res.get("profile") or {}
        found += bool(res.get("found"))
        extra = [str(bool(res.get("found"))).lower()] + [p.get(k, "") for k in CSV_FIELDS[1:-1]]
        extra.append(f"https://www.linkedin.com/in/{p['slug']}" if p.get("slug") else "")
        if res.get("status") == "error":
            extra[0] = f"error: {res.get('error')}"
        out_rows.append(r + extra)
    out = args.output or args.input.rsplit(".", 1)[0] + "-enriched.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header + [f"eg_{k}" for k in CSV_FIELDS])
        w.writerows(out_rows)
    print(f"Done: {found} of {len(todo)} found (not-found rows are free). Saved {out}", file=sys.stderr)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="enrichgrid", description="EnrichGrid LinkedIn profile & company scraper API")
    ap.add_argument("--api-key", help="defaults to $ENRICHGRID_KEY")
    ap.add_argument("--base-url", default="https://enrichgrid.com", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"enrichgrid {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for kind in ("person", "company", "school"):
        p = sub.add_parser(kind, help=f"enrich a {kind} (LinkedIn URL, slug or ID)")
        p.add_argument("identifier")
        p.add_argument("--live", action="store_true", help="skip the 30-day copy and collect it again")
    s = sub.add_parser("search", help="search people; prints profile slugs")
    s.add_argument("filters", help='JSON, e.g. \'{"title": {"any": ["cto"]}, "country": ["US"]}\'')
    s.add_argument("--pages", type=int, default=1, help="how many pages of 10 (1 credit per page with results)")
    c = sub.add_parser("csv", help="enrich the LinkedIn profile URLs in a CSV file")
    c.add_argument("input")
    c.add_argument("-o", "--output")
    c.add_argument("--column", help="column with the LinkedIn URLs (auto-detected)")
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--live", action="store_true")
    sub.add_parser("me", help="credit balance and plan")
    args = ap.parse_args(argv)
    try:
        client = Client(args.api_key, args.base_url)
        if args.cmd in ("person", "company", "school"):
            _print(getattr(client, args.cmd)(args.identifier, live=args.live))
        elif args.cmd == "search":
            _print(list(client.search_all(json.loads(args.filters), max_pages=args.pages)))
        elif args.cmd == "csv":
            return cmd_csv(client, args)
        else:
            _print(client.me())
        return 0
    except (EnrichGridError, ValueError) as exc:
        print(f"enrichgrid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
