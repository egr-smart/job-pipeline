"""The `digest` stage: shortlist -> markdown report -> notifiers (file, email)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import markdown

from .config import Config
from .rank import Item, Sections

SOURCE_LABELS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "hn": "HN",
    "remotive": "Remotive", "weworkremotely": "We Work Remotely",
}
LOCATION_MARK = {"yes": "✓", "unclear": "?", "no": "✗"}


@dataclass
class Digest:
    subject: str
    markdown: str
    shortlisted: list[tuple[int, str]]  # (job_id, list name) for appearance tracking

    @property
    def html(self) -> str:
        body = markdown.markdown(self.markdown, extensions=["sane_lists"])
        return HTML_TEMPLATE.format(title=self.subject, body=body)


def _age(posted_at: str | None, now: datetime) -> str:
    if not posted_at:
        return ""
    posted = datetime.fromisoformat(posted_at)
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    days = (now - posted).days
    return "posted today" if days <= 0 else f"posted {days}d ago"


def _md_escape(s: str) -> str:
    return s.replace("[", "\\[").replace("]", "\\]").replace("*", "\\*").replace("_", "\\_")


def render_item(n: int, item: Item, now: datetime) -> str:
    j, r = item.job, item.result
    (_, main_url), *others = item.links
    lines = [
        f"### {n}. [{_md_escape(j['title'])}]({main_url}) — {_md_escape(j['company'])}",
        "",
        f"**Score {item.score}** · role {r.role_fit} · stack {r.stack_fit} · seniority {r.seniority}"
        + (f" · {_age(j['posted_at'], now)}" if j["posted_at"] else "")
        + f" · `#{j['id']}`",
        "",
        f"- **Location:** “{_md_escape(j['location_raw'] or 'not stated')}” {LOCATION_MARK[r.location_eligible]} "
        f"— {_md_escape(r.location_note)}",
    ]
    if salary := (r.salary_text or j["salary_raw"]):
        lines.append(f"- **Salary:** {_md_escape(salary)}")
    lines.append(f"- **Why:** {_md_escape(r.summary)}")
    if r.gaps:
        lines.append(f"- **Gaps:** {_md_escape('; '.join(r.gaps))}")
    if r.red_flags:
        lines.append(f"- **Flags:** {_md_escape('; '.join(r.red_flags))}")
    if item.dropped:
        lines.append(f"- **Dropped:** {item.dropped}")
    if others:
        also = " · ".join(f"[{SOURCE_LABELS.get(s, s)}]({u})" for s, u in others)
        lines.append(f"- **Also listed on:** {also}")
    return "\n".join(lines)


def render_health(run: dict | None) -> str:
    if not run:
        return "_No collect run recorded yet._"
    stats = json.loads(run["stats_json"])
    sources = stats.get("sources", [])
    failed = [s for s in sources if s.get("error")]
    r = stats.get("results", {})
    lines = [
        f"- **Last collect:** {run['started_at']} ({run['kind']})",
        f"- **Sources:** {len(sources) - len(failed)}/{len(sources)} ok"
        + (f"; failed: {', '.join(s['name'] for s in failed)}" if failed else ""),
        f"- **Pipeline:** {stats.get('fetched', 0)} fetched → {stats.get('new', 0)} new → "
        f"{r.get('allow', 0) + r.get('unknown', 0)} passed filters → {stats.get('scored', 0)} scored"
        + (f" ({stats['deferred']} deferred)" if stats.get("deferred") else ""),
    ]
    if stats.get("scorer_id"):
        cost = stats.get("cost_usd") or 0
        lines.append(f"- **LLM:** {stats['scorer_id']}" + (f" · ≈${cost:.2f}" if cost else ""))
    if stats.get("scoring_aborted"):
        lines.append(f"- **Scoring stopped early:** {stats['scoring_aborted']}")
    if stats.get("score_failures"):
        lines.append(f"- **Scoring failures:** {len(stats['score_failures'])}")
    return "\n".join(lines)


def build_digest(
    cfg: Config, sections: Sections, last_run, *, include_dropped: bool = False, now: datetime | None = None
) -> Digest:
    now = now or datetime.now(UTC)
    top = sections.top[: cfg.ranking.shortlist_size]
    unclear = sections.unclear[: cfg.ranking.unclear_list_size]
    date = now.strftime("%-d %b %Y")
    subject = f"Job digest — {len(top)} matches, {len(unclear)} worth asking ({date})"

    parts = [f"# {subject}", ""]
    parts += ["## Top matches", ""]
    parts += [render_item(n, i, now) + "\n" for n, i in enumerate(top, 1)] or ["_No new matches this time._\n"]
    parts += ["## Worth asking (location unclear)", ""]
    parts += [render_item(n, i, now) + "\n" for n, i in enumerate(unclear, 1)] or ["_None._\n"]
    if include_dropped and sections.dropped:
        parts += ["## Dropped", ""]
        parts += [render_item(n, i, now) + "\n" for n, i in enumerate(sections.dropped, 1)]
    parts += ["## Run health", "", render_health(last_run), ""]
    parts += [
        "---",
        "Mark a job with `jobs mark <id> applied|dismissed` to stop seeing it. "
        "Remotive and We Work Remotely listings are credited to and linked back to their source.",
    ]
    shortlisted = [(i.job["id"], "top") for i in top] + [(i.job["id"], "unclear") for i in unclear]
    return Digest(subject, "\n".join(parts), shortlisted)


HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 760px; margin: 0 auto; padding: 24px 16px; color: #1f2328; background: #fff; }}
  h1 {{ font-size: 22px; margin-bottom: 8px; }}
  h2 {{ font-size: 18px; margin-top: 32px; padding-bottom: 4px; border-bottom: 1px solid #d0d7de; }}
  h3 {{ font-size: 16px; margin: 24px 0 4px; }}
  h3 a {{ color: #0969da; text-decoration: none; }}
  ul {{ padding-left: 20px; margin-top: 4px; }}
  li {{ margin: 2px 0; }}
  code {{ background: #f6f8fa; padding: 1px 4px; border-radius: 4px; font-size: 13px; }}
  hr {{ border: none; border-top: 1px solid #d0d7de; margin-top: 32px; }}
  p {{ margin: 4px 0; }}
</style></head>
<body>
{body}
</body></html>
"""
