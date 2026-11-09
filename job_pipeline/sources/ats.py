"""Public job-board APIs of the three common ATS platforms (Greenhouse, Lever, Ashby)."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from ..http import Http
from ..models import RawJob, Workplace
from ..text import html_to_text

Ats = Literal["greenhouse", "lever", "ashby"]

BOARD_URLS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true&pay_transparency=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true",
}


def _iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _workplace(raw: str | None) -> Workplace | None:
    raw = (raw or "").lower().replace("-", "").replace("_", "")
    return {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(raw)


def _money(amount: float | int | None) -> str:
    if amount is None:
        return "?"
    return f"{amount / 1000:g}k" if amount >= 1000 else f"{amount:g}"


def _cents(v: int | None) -> float | None:
    return v / 100 if v is not None else None


def parse_greenhouse(data: dict, company: str) -> list[RawJob]:
    jobs = []
    for j in data.get("jobs", []):
        # Ranges are often per-country ("United States Salary Range"), so keep their labels.
        salary = "; ".join(
            f"{r.get('currency_type', '')} {_money(_cents(r.get('min_cents')))}–"
            f"{_money(_cents(r.get('max_cents')))}" + (f" ({r['title']})" if r.get("title") else "")
            for r in j.get("pay_input_ranges") or []
        ).strip() or None
        jobs.append(
            RawJob(
                source="greenhouse",
                source_id=str(j["id"]),
                url=j["absolute_url"],
                company=company,
                title=j["title"],
                location_raw=(j.get("location") or {}).get("name", ""),
                description_text=html_to_text(j.get("content"), unescape=True),
                posted_at=_iso(j.get("first_published") or j.get("updated_at")),
                salary_raw=salary,
            )
        )
    return jobs


def parse_lever(data: list, company: str) -> list[RawJob]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locations = cats.get("allLocations") or [cats.get("location") or ""]
        parts = [j.get("descriptionPlain") or ""]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content'))}")
        parts.append(j.get("additionalPlain") or "")
        salary = None
        if sr := j.get("salaryRange"):
            salary = f"{sr.get('currency', '')} {_money(sr.get('min'))}–{_money(sr.get('max'))} {sr.get('interval', '')}".strip()
        created = j.get("createdAt")
        jobs.append(
            RawJob(
                source="lever",
                source_id=j["id"],
                url=j["hostedUrl"],
                company=company,
                title=j["text"],
                location_raw="; ".join(l for l in locations if l),
                description_text="\n\n".join(p.strip() for p in parts if p.strip()),
                posted_at=datetime.fromtimestamp(created / 1000, UTC) if created else None,
                salary_raw=salary,
                workplace=_workplace(j.get("workplaceType")),
            )
        )
    return jobs


def parse_ashby(data: dict, company: str) -> list[RawJob]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""] + [
            s.get("location", "") for s in j.get("secondaryLocations") or []
        ]
        workplace = _workplace(j.get("workplaceType"))
        if workplace is None and j.get("isRemote"):
            workplace = "remote"
        comp = j.get("compensation") or {}
        jobs.append(
            RawJob(
                source="ashby",
                source_id=j["id"],
                url=j["jobUrl"],
                company=company,
                title=j["title"],
                location_raw="; ".join(l for l in locations if l),
                description_text=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                posted_at=_iso(j.get("publishedAt")),
                salary_raw=comp.get("scrapeableCompensationSalarySummary")
                or comp.get("compensationTierSummary"),
                workplace=workplace,
            )
        )
    return jobs


PARSERS: dict[str, Callable[..., list[RawJob]]] = {
    "greenhouse": parse_greenhouse,
    "lever": parse_lever,
    "ashby": parse_ashby,
}


class BoardNotFound(Exception):
    pass


def fetch_board(http: Http, ats: Ats, slug: str, company: str) -> list[RawJob]:
    resp = http.get(BOARD_URLS[ats].format(slug=slug))
    if resp.status_code == 404:
        raise BoardNotFound(f"{ats}/{slug}")
    resp.raise_for_status()
    return PARSERS[ats](resp.json(), company)
