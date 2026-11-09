"""Watchlist of companies polled directly via their ATS job board."""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from ..http import Http
from . import SourceResult
from .ats import BOARD_URLS, Ats, fetch_board

_SUFFIXES = r"\b(inc|llc|ltd|limited|pty|gmbh|corp|corporation|co|company|labs|technologies|foundation|hq)\b"


class Company(BaseModel):
    name: str
    ats: Ats | None = None
    slug: str | None = None


class Watchlist(BaseModel):
    companies: list[Company] = []


def load_watchlist(path: Path) -> Watchlist:
    if not path.exists():
        return Watchlist()
    return Watchlist.model_validate(yaml.safe_load(path.read_text()) or {})


def save_watchlist(path: Path, wl: Watchlist) -> None:
    data = {"companies": [c.model_dump(exclude_none=True) for c in wl.companies]}
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def slug_candidates(name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", name.lower())
    stripped = re.sub(r"\s+", " ", re.sub(_SUFFIXES, "", base)).strip()
    out: list[str] = []
    for variant in (base, stripped):
        words = variant.split()
        for cand in ("".join(words), "-".join(words), words[0] if words else ""):
            if cand and cand not in out:
                out.append(cand)
    return out


def resolve_company(http: Http, company: Company) -> list[tuple[Ats, str, int]]:
    """Find boards that might be this company's. Returns [(ats, slug, job_count)], best first.

    All three APIs return 404 for unknown boards, but slugs are first-come names: "neon" on
    Lever is a Brazilian bank. So every hit is returned, and callers flag more than one as
    ambiguous. Boards with jobs sort before empty ones.
    """
    slugs = [company.slug] if company.slug else slug_candidates(company.name)
    atses: list[Ats] = [company.ats] if company.ats else list(BOARD_URLS)  # type: ignore[list-item]
    hits = []
    for slug in slugs:
        for ats in atses:
            try:
                hits.append((ats, slug, len(fetch_board(http, ats, slug, company.name))))
            except Exception:  # BoardNotFound, or a transient error on a wrong guess
                continue
    return sorted(hits, key=lambda h: h[2] == 0)


def fetch_watchlist(http: Http, wl: Watchlist) -> list[SourceResult]:
    results = []
    for c in wl.companies:
        name = f"{c.ats or '?'}:{c.slug or c.name}"
        if not (c.ats and c.slug):
            results.append(SourceResult(name, error="unresolved (run `jobs watchlist resolve`)"))
            continue
        try:
            results.append(SourceResult(name, jobs=fetch_board(http, c.ats, c.slug, c.name)))
        except Exception as e:  # one broken board never fails the run
            results.append(SourceResult(name, error=f"{type(e).__name__}: {e}"))
    return results
