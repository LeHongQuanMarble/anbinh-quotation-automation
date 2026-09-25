"""Mac mini agent: pulls quote jobs from the web app, renders the file, runs the Codex review, uploads.

Pull model (agent -> server over HTTPS) instead of server -> Mac mini push:
  * the Mac mini needs no inbound port / public IP (it sits behind the office NAT);
  * if the Mac mini is offline, jobs simply wait in the queue as PENDING - nothing is lost;
  * the server stays the single source of truth for job status.

Run:  python -m agent.agent            (loop forever)
      python -m agent.agent --once     (process at most one job, used by tests)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import threading
import time

from common.config import load_env

load_env()

import requests  # noqa: E402

from agent import codex_step  # noqa: E402
from agent.renderer import PermanentError, docx_text, render_quote  # noqa: E402
from common.quote_logic import QuoteValidationError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [agent] %(message)s")
log = logging.getLogger("agent")

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
WORKER_ID = os.environ.get("WORKER_ID", "mac-mini-01")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
WORK_DIR = os.environ.get("AGENT_WORK_DIR", "agent_work")
# Demo switch: make the first attempt of every job crash, to show the retry path.
SIMULATE_FAILURE = os.environ.get("SIMULATE_FAILURE", "") == "1"


class Api:
    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self.s.headers["Authorization"] = f"Bearer {AGENT_TOKEN}"

    def post(self, path, **kw):
        return self.s.post(f"{SERVER_URL}{path}", timeout=30, **kw)

    def claim(self):
        r = self.post("/api/agent/claim", json={"worker_id": WORKER_ID})
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def heartbeat(self, job):
        return self.post(f"/api/agent/jobs/{job['job_id']}/heartbeat",
                         json={"worker_id": WORKER_ID, "attempt": job["attempt"]}).status_code == 200

    def complete(self, job, path, review):
        with open(path, "rb") as f:
            data = f.read()
        r = self.post(f"/api/agent/jobs/{job['job_id']}/complete",
                      data={"worker_id": WORKER_ID, "attempt": job["attempt"],
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "ai_review": json.dumps(review, ensure_ascii=False) if review else ""},
                      files={"file": (os.path.basename(path), data)})
        if r.status_code == 409:  # our lease expired and someone else owns the job now: drop our result
            log.warning("result for %s discarded by server (lease lost)", job["quote_no"])
            return
        r.raise_for_status()

    def fail(self, job, error, retryable):
        self.post(f"/api/agent/jobs/{job['job_id']}/fail",
                  json={"worker_id": WORKER_ID, "attempt": job["attempt"], "error": error, "retryable": retryable})


def with_retries(fn, what, attempts=4):
    """Retry transient network errors with exponential backoff + jitter."""
    for i in range(attempts):
        try:
            return fn()
        except requests.RequestException as e:
            if i == attempts - 1:
                raise
            delay = min(30, 2 ** i) + random.random()
            log.warning("%s failed (%s), retrying in %.1fs", what, e, delay)
            time.sleep(delay)


def process(api: Api, job: dict) -> None:
    log.info("processing %s (attempt %s)", job["quote_no"], job["attempt"])
    stop = threading.Event()

    def keep_lease():
        # Long jobs (e.g. a slow Codex call) keep their lease alive; if the lease is lost we stop.
        while not stop.wait(job["lease_seconds"] / 3):
            try:
                if not api.heartbeat(job):
                    log.warning("lease lost for %s", job["quote_no"])
                    return
            except requests.RequestException:
                pass

    threading.Thread(target=keep_lease, daemon=True).start()
    try:
        if SIMULATE_FAILURE and job["attempt"] == 1:
            raise RuntimeError("Giả lập lỗi giữa chừng (SIMULATE_FAILURE=1)")
        path, _ = render_quote(job["payload"], os.path.join(WORK_DIR, "out"))
        review = codex_step.review(job["payload"], docx_text(path))
        with_retries(lambda: api.complete(job, path, review), "upload")
        log.info("done %s -> %s", job["quote_no"], path)
    except (PermanentError, QuoteValidationError) as e:
        log.error("permanent failure %s: %s", job["quote_no"], e)
        with_retries(lambda: api.fail(job, str(e), retryable=False), "report failure")
    except Exception as e:
        log.exception("transient failure %s", job["quote_no"])
        with_retries(lambda: api.fail(job, f"{type(e).__name__}: {e}", retryable=True), "report failure")
    finally:
        stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    api = Api()
    log.info("agent %s polling %s (codex mode: %s)", WORKER_ID, SERVER_URL, os.environ.get("CODEX_MODE", "mock"))
    backoff = POLL_INTERVAL
    while True:
        try:
            job = api.claim()
            backoff = POLL_INTERVAL
        except requests.RequestException as e:
            # Server unreachable / Mac mini offline: just wait and try again; jobs stay queued server-side.
            log.warning("cannot reach server: %s", e)
            job, backoff = None, min(60, backoff * 2)
        if job:
            process(api, job)
            if args.once:
                return
            continue
        if args.once:
            return
        time.sleep(backoff)


if __name__ == "__main__":
    main()
