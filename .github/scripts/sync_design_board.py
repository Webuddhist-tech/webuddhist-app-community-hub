"""Sync the WeBuddhist Design project board (GitHub Projects #156) into a Notion database."""
import json, os, sys, time, urllib.request, urllib.error

ORG = os.environ.get("PROJECT_ORG", "Webuddhist-tech")
PROJECT_NUMBER = int(os.environ.get("PROJECT_NUMBER", "156"))
TEAM = [m.strip().lower() for m in os.environ.get("TEAM_MEMBERS", "palden4,evanyerburgh,lod0001").split(",")]
GH_TOKEN = os.environ["PROJECT_TOKEN"]
DRY_RUN = os.environ.get("DRY_RUN") == "1"
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DB = os.environ.get("NOTION_DATABASE_ID")
NOTION_VERSION = "2022-06-28"

# Board field name -> Notion property name
STATUS_FIELD, SPRINT_FIELD, ESTIMATE_FIELD = "Status", "Sprint", "Estimate"


def http(url, payload=None, headers=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt + 1)
                continue
            sys.exit(f"HTTP {e.code} on {url}: {e.read().decode()[:500]}")


# ---------- GitHub ----------

QUERY = """
query($org:String!, $num:Int!, $after:String) {
  organization(login:$org) { projectV2(number:$num) {
    items(first:100, after:$after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id updatedAt
        content {
          __typename
          ... on Issue { title number url state createdAt closedAt repository { name }
                         assignees(first:10) { nodes { login } } }
          ... on PullRequest { title number url state createdAt closedAt repository { name }
                               assignees(first:10) { nodes { login } } }
          ... on DraftIssue { title createdAt assignees(first:10) { nodes { login } } }
        }
        fieldValues(first:30) { nodes {
          __typename
          ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
          ... on ProjectV2ItemFieldIterationValue { title startDate duration field { ... on ProjectV2FieldCommon { name } } }
          ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
          ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
        } }
      }
    }
  } }
}"""


def fetch_items():
    items, after = [], None
    while True:
        res = http("https://api.github.com/graphql",
                   {"query": QUERY, "variables": {"org": ORG, "num": PROJECT_NUMBER, "after": after}},
                   {"Authorization": f"Bearer {GH_TOKEN}"})
        if res.get("errors"):
            sys.exit(f"GraphQL error: {res['errors']}")
        page = res["data"]["organization"]["projectV2"]["items"]
        items += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return items
        after = page["pageInfo"]["endCursor"]


def to_row(item):
    c = item.get("content") or {}
    assignees = [a["login"] for a in (c.get("assignees") or {}).get("nodes", [])]
    tracked = [a for a in assignees if a.lower() in TEAM]
    if not tracked:
        return None
    row = {
        "id": item["id"],
        "title": c.get("title") or "(untitled)",
        "type": {"Issue": "Issue", "PullRequest": "PR", "DraftIssue": "Draft"}.get(c.get("__typename"), "Other"),
        "repo": (c.get("repository") or {}).get("name"),
        "number": c.get("number"),
        "url": c.get("url"),
        "state": (c.get("state") or "DRAFT").title(),
        "assignees": tracked,
        "created": (c.get("createdAt") or "")[:10] or None,
        "closed": (c.get("closedAt") or "")[:10] or None,
        "updated": item["updatedAt"][:10],
        "status": None, "sprint": None, "sprint_start": None, "estimate": None, "dates": {},
    }
    for fv in item["fieldValues"]["nodes"]:
        name = (fv.get("field") or {}).get("name")
        t = fv["__typename"]
        if t == "ProjectV2ItemFieldSingleSelectValue" and name == STATUS_FIELD:
            row["status"] = fv["name"]
        elif t == "ProjectV2ItemFieldIterationValue" and name == SPRINT_FIELD:
            row["sprint"], row["sprint_start"] = fv["title"], fv["startDate"]
        elif t == "ProjectV2ItemFieldNumberValue" and name == ESTIMATE_FIELD:
            row["estimate"] = fv["number"]
        elif t == "ProjectV2ItemFieldDateValue" and name:
            row["dates"][name] = fv["date"]
    return row


# ---------- Notion ----------

def notion(path, payload=None, method=None):
    time.sleep(0.35)  # stay under ~3 req/s
    return http(f"https://api.notion.com/v1/{path}", payload, {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }, method)


def ensure_schema(date_fields):
    db = notion(f"databases/{NOTION_DB}")
    title_prop = next(k for k, v in db["properties"].items() if v["type"] == "title")
    wanted = {
        "Item ID": {"rich_text": {}}, "Type": {"select": {}}, "Repo": {"select": {}},
        "Issue #": {"number": {}}, "URL": {"url": {}}, "Status": {"select": {}},
        "Sprint": {"select": {}}, "Sprint start": {"date": {}}, "Assignees": {"multi_select": {}},
        "Estimate": {"number": {}}, "State": {"select": {}}, "Created": {"date": {}},
        "Closed": {"date": {}}, "Updated": {"date": {}},
        **{d: {"date": {}} for d in date_fields},
    }
    missing = {k: v for k, v in wanted.items() if k not in db["properties"]}
    if missing:
        notion(f"databases/{NOTION_DB}", {"properties": missing}, "PATCH")
    return title_prop


def existing_pages():
    pages, cursor = {}, None
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        res = notion(f"databases/{NOTION_DB}/query", body, "POST")
        for p in res["results"]:
            rt = p["properties"].get("Item ID", {}).get("rich_text", [])
            if rt:
                pages[rt[0]["plain_text"]] = p["id"]
        if not res.get("has_more"):
            return pages
        cursor = res["next_cursor"]


def props(row, title_prop):
    sel = lambda v: {"select": {"name": v}} if v else {"select": None}
    date = lambda v: {"date": {"start": v}} if v else {"date": None}
    p = {
        title_prop: {"title": [{"text": {"content": row["title"][:2000]}}]},
        "Item ID": {"rich_text": [{"text": {"content": row["id"]}}]},
        "Type": sel(row["type"]), "Repo": sel(row["repo"]),
        "Issue #": {"number": row["number"]}, "URL": {"url": row["url"]},
        "Status": sel(row["status"]), "Sprint": sel(row["sprint"]),
        "Sprint start": date(row["sprint_start"]),
        "Assignees": {"multi_select": [{"name": a} for a in row["assignees"]]},
        "Estimate": {"number": row["estimate"]}, "State": sel(row["state"]),
        "Created": date(row["created"]), "Closed": date(row["closed"]), "Updated": date(row["updated"]),
    }
    for name, d in row["dates"].items():
        p[name] = date(d)
    return p


def main():
    rows = [r for r in map(to_row, fetch_items()) if r]
    print(f"{len(rows)} board items assigned to {', '.join(TEAM)}")
    if DRY_RUN:
        for r in rows:
            print(f"- [{r['status']}] {r['sprint'] or '-'} | {r['repo']}#{r['number']} {r['title']} ({', '.join(r['assignees'])})")
        return
    date_fields = sorted({d for r in rows for d in r["dates"]})
    title_prop = ensure_schema(date_fields)
    pages = existing_pages()
    created = updated = 0
    for r in rows:
        if r["id"] in pages:
            notion(f"pages/{pages.pop(r['id'])}", {"properties": props(r, title_prop)}, "PATCH")
            updated += 1
        else:
            notion("pages", {"parent": {"database_id": NOTION_DB}, "properties": props(r, title_prop)}, "POST")
            created += 1
    for page_id in pages.values():  # left the board or no longer assigned to the team
        notion(f"pages/{page_id}", {"archived": True}, "PATCH")
    print(f"Notion: {created} created, {updated} updated, {len(pages)} archived")


if __name__ == "__main__":
    main()
