# enrichgrid

**Official Python client and CLI for [EnrichGrid](https://enrichgrid.com/?ref=pypi), the LinkedIn profile & company scraper API.**
Send a LinkedIn profile, company or school URL and get clean JSON back: current role and seniority, the full work history with each employer's firmographics, education, skills, followers and connections. People search finds profiles by title, company, location and more.

- No LinkedIn login or cookies.
- 1 credit per found record. Lookups that find nothing are free.
- Slow profiles are polled for you (free). 429/503 are retried using `Retry-After`.
- 50 free credits at signup, no card: https://enrichgrid.com/signup

```bash
pip install enrichgrid
export ENRICHGRID_KEY=...   # https://enrichgrid.com/dashboard/keys
```

## Python

```python
from enrichgrid import Client

eg = Client()  # reads ENRICHGRID_KEY

lookup = eg.person("https://www.linkedin.com/in/some-profile")
if lookup["found"]:
    p = lookup["profile"]
    print(p["first_name"], p["last_name"], "-", p["current_title"], "at", p["current_company_name"])
    for job in p["sections"]["experiences"]:
        print(job["title"], job["company_name"], job["started_on"], job.get("ended_on"))

company = eg.company("https://www.linkedin.com/company/some-company")["company"]
print(company["industry"], company["staff_count_range"], company["hq_city"])

# people search: 10 slugs per page, 1 credit per page with results
for slug in eg.search_all({"title": {"any": ["cto"]}, "country": ["DE"], "company_size": {"min": 11, "max": 200}},
                          max_pages=3):
    print(slug)

# many profiles in parallel; results keep the input order, one bad row doesn't stop the batch
results = eg.enrich_many(["slug-one", "https://www.linkedin.com/in/slug-two"], workers=4)

print(eg.me())  # credits, plan, requests per second
```

Errors: `AuthError` (401/403), `InsufficientCredits` (402), `InvalidRequest` (400/404/422), `Unavailable` (429/503 after retries, or network). All of them subclass `EnrichGridError` with `.status`, `.code` and `.message`.

## Command line

```bash
enrichgrid person https://www.linkedin.com/in/some-profile
enrichgrid company https://www.linkedin.com/company/some-company
enrichgrid search '{"title": {"any": ["head of sales"]}, "country": ["US"]}' --pages 2
enrichgrid csv leads.csv -o leads-enriched.csv --workers 4
enrichgrid me
```

`enrichgrid csv` finds the LinkedIn URL column on its own, or you can name it with `--column`. It keeps every original row and column and adds `eg_found`, `eg_first_name`, `eg_current_title`, `eg_current_company_name`, `eg_location`, `eg_num_connections` and more. For files with thousands of rows, the [web bulk upload](https://enrichgrid.com/bulk-linkedin-enrichment) does the same with no code.

## Notes

- `live=True` skips the 30-day copy and collects the profile again. It costs the same credit.
- Requests per second depend on your plan: 2 on free, 5–50 on paid plans. Keep `workers` near that number.
- Coming from Proxycurl? See the [migration guide](https://enrichgrid.com/guides/proxycurl-migration).

Full API reference: https://enrichgrid.com/api-docs. MIT license. EnrichGrid is independent and not affiliated with LinkedIn.
