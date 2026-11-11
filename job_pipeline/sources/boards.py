"""Aggregator sources: HN "Who is hiring?", Remotive and We Work Remotely."""

import html
import re
from datetime import UTC, datetime

import feedparser

from ..http import Http
from ..models import RawJob
from ..text import html_to_text
from . import SourceResult

# --- Hacker News "Ask HN: Who is hiring?" -------------------------------------------------

HN_THREADS_URL = "https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
HN_ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"


def hn_thread_ids(http: Http, count: int) -> list[str]:
    """The newest `count` "Who is hiring?" threads (not "Who wants to be hired?")."""
    resp = http.get(HN_THREADS_URL)
    resp.raise_for_status()
    hits = [h for h in resp.json()["hits"] if h["title"].lower().startswith("ask hn: who is hiring")]
    return [h["objectID"] for h in hits[:count]]


def parse_hn_comment(c: dict) -> RawJob | None:
    """One top-level comment -> one job.

    By convention the first line is a header like "Company | Role(s) | REMOTE (US) | Full-time".
    The header serves as the location and the part after the company as the title, so
    the normal filters work on it. One comment can advertise several roles; the scorer reads the full text.
    """
    text = c.get("text")
    if not text:  # deleted / dead
        return None
    raw = html.unescape(text)
    header = html_to_text(re.split(r"<p>", raw, maxsplit=1)[0]).replace("\n", " ").strip()
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if len(parts) < 2:
        return None  # not in the conventional format; skip rather than guess
    rest = " | ".join(parts[1:])
    return RawJob(
        source="hn",
        source_id=str(c["id"]),
        url=f"https://news.ycombinator.com/item?id={c['id']}",
        company=parts[0][:100],
        title=rest[:200],
        location_raw=header[:300],  # whole header: some put the place first ("Berlin | Acme | ...")
        description_text=html_to_text(raw),
        posted_at=datetime.fromtimestamp(c["created_at_i"], UTC) if c.get("created_at_i") else None,
    )


def fetch_hn(http: Http, threads: int = 2) -> SourceResult:
    try:
        jobs = []
        for thread_id in hn_thread_ids(http, threads):
            resp = http.get(HN_ITEM_URL.format(id=thread_id))
            resp.raise_for_status()
            jobs += [j for c in resp.json().get("children", []) if (j := parse_hn_comment(c))]
        return SourceResult("hn", jobs=jobs)
    except Exception as e:
        return SourceResult("hn", error=f"{type(e).__name__}: {e}")


# --- Remotive ------------------------------------------------------------------------------
# Public API terms: link back to Remotive and credit it as the source; fetch at most ~4x/day.
# As of Nov 2026 the public feed is a small sample (~15 jobs) and ignores category filters.

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"


def parse_remotive(data: dict) -> list[RawJob]:
    jobs = []
    for j in data.get("jobs", []):
        posted = j.get("publication_date")
        jobs.append(
            RawJob(
                source="remotive",
                source_id=str(j["id"]),
                url=j["url"],
                company=j.get("company_name") or "",
                title=j["title"],
                location_raw=j.get("candidate_required_location") or "",
                description_text=html_to_text(j.get("description")),
                posted_at=datetime.fromisoformat(posted).replace(tzinfo=UTC) if posted else None,
                salary_raw=j.get("salary") or None,
                workplace="remote",
            )
        )
    return jobs


def fetch_remotive(http: Http) -> SourceResult:
    try:
        resp = http.get(REMOTIVE_URL)
        resp.raise_for_status()
        return SourceResult("remotive", jobs=parse_remotive(resp.json()))
    except Exception as e:
        return SourceResult("remotive", error=f"{type(e).__name__}: {e}")


# --- We Work Remotely ----------------------------------------------------------------------

WWR_URL = "https://weworkremotely.com/categories/remote-{category}-jobs.rss"
_FLAG_EMOJI = re.compile("[\U0001F1E6-\U0001F1FF]")


def parse_wwr(feed_text: str) -> list[RawJob]:
    jobs = []
    for e in feedparser.parse(feed_text).entries:
        company, _, title = e.title.partition(": ")
        if not title:
            company, title = "", e.title
        # `region` can say "Anywhere in the World" while `country` lists the only allowed
        # countries, so the country list wins when present.
        country = _FLAG_EMOJI.sub("", e.get("country", "")).strip()
        location = country or e.get("region", "")
        posted = e.get("published_parsed")
        jobs.append(
            RawJob(
                source="weworkremotely",
                source_id=e.id,
                url=e.link,
                company=company.strip(),
                title=title.strip(),
                location_raw=location,
                description_text=html_to_text(e.get("summary")),
                posted_at=datetime(*posted[:6], tzinfo=UTC) if posted else None,
                workplace="remote",
            )
        )
    return jobs


def fetch_wwr(http: Http, categories: list[str]) -> list[SourceResult]:
    results = []
    for cat in categories:
        name = f"weworkremotely:{cat}"
        try:
            resp = http.get(WWR_URL.format(category=cat))
            resp.raise_for_status()
            results.append(SourceResult(name, jobs=parse_wwr(resp.text)))
        except Exception as e:
            results.append(SourceResult(name, error=f"{type(e).__name__}: {e}"))
    return results
