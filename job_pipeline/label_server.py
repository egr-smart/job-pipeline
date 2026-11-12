"""`jobs eval label`: a local web page for labelling jobs into the eval set.

Serves on 127.0.0.1 only. Labels are written with the same EvalSet code as `jobs eval add`.
Model scores are deliberately not shown, so they don't anchor your judgement.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources

from .evals import EvalSet
from .store import Store

SOURCE_LABELS = {
    "greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "hn": "HN",
    "remotive": "Remotive", "weworkremotely": "We Work Remotely",
}


def pool(store: Store, evals: EvalSet) -> list[dict]:
    """Every stored job that passed the filters, with its current label if any."""
    labels = {l.id: l for l in evals.labels()}
    rows = store.db.execute(
        """
        SELECT id, filter_result, filter_reason FROM jobs
        WHERE filter_result IN ('allow', 'unknown')
        ORDER BY last_seen DESC, id DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        job = store.get_job(r["id"])
        label = labels.get(r["id"])
        out.append({
            "id": r["id"],
            "company": job.company,
            "title": job.title,
            "location": job.location_raw,
            "workplace": job.workplace,
            "salary": job.salary_raw,
            "posted": job.posted_at.date().isoformat() if job.posted_at else None,
            "filter": f"{r['filter_result']}: {r['filter_reason']}",
            "links": [{"source": SOURCE_LABELS.get(l["source"], l["source"]), "url": l["url"]}
                      for l in store.listings(r["id"])],
            "description": job.description_text,
            "label": label.model_dump(exclude_none=True) if label else None,
        })
    # Labelled jobs that no longer pass the filters still show, so you can review them.
    for job_id, label in labels.items():
        if not any(j["id"] == job_id for j in out):
            snap = evals.snapshot(job_id)
            out.append({
                "id": job_id, "company": snap.job.company, "title": snap.job.title,
                "location": snap.job.location_raw, "workplace": snap.job.workplace,
                "salary": snap.job.salary_raw,
                "posted": snap.job.posted_at.date().isoformat() if snap.job.posted_at else None,
                "filter": f"{snap.filter_result}: {snap.filter_reason} (snapshot)",
                "links": [{"source": SOURCE_LABELS.get(snap.job.source, snap.job.source), "url": snap.job.url}],
                "description": snap.job.description_text,
                "label": label.model_dump(exclude_none=True),
            })
    return out


def make_handler(store: Store, evals: EvalSet):
    page = resources.files("job_pipeline").joinpath("label_page.html").read_text()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            if self.path == "/":
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/jobs":
                self._json(200, pool(store, evals))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            # Requiring a JSON content type means other websites can't post here: a
            # cross-origin JSON request needs a CORS preflight, which this server never grants.
            if self.headers.get("Content-Type") != "application/json":
                return self._json(415, {"error": "expected application/json"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                job_id = int(body["id"])
                if self.path == "/api/label":
                    verdict = body["verdict"]
                    location = body.get("location") or None
                    note = (body.get("note") or "").strip() or None
                    if verdict not in ("strong", "maybe", "no") or location not in (None, "yes", "no", "unclear"):
                        return self._json(400, {"error": "bad verdict or location"})
                    label = evals.add(store, job_id, verdict, location, note)
                    return self._json(200, label.model_dump(exclude_none=True))
                if self.path == "/api/unlabel":
                    evals.remove(job_id)
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "not found"})
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": f"bad request: {e}"})

    return Handler


def serve(store: Store, evals: EvalSet, port: int) -> HTTPServer:
    # Single-threaded on purpose: the SQLite connection belongs to this thread.
    return HTTPServer(("127.0.0.1", port), make_handler(store, evals))
