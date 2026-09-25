"""Internal management web app (simulated) + agent API used by the Mac mini."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from common.config import load_env

load_env()

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import db  # noqa: E402
from common.quote_logic import QuoteValidationError, format_vnd, validate_and_compute  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [web] %(message)s")
log = logging.getLogger("web")

AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
STORAGE_DIR = os.environ.get("STORAGE_DIR", "storage/quotes")
SESSION_TTL = 8 * 3600
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
FORM_ROWS = 5



@asynccontextmanager
async def lifespan(_app):
    if not AGENT_TOKEN or AGENT_TOKEN.startswith("change-me"):
        log.warning("AGENT_TOKEN is not set to a strong secret - OK for local demo only")
    os.makedirs(STORAGE_DIR, exist_ok=True)
    conn = db.connect()
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(title="An Bình Chemtech - Quotation prototype", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.filters["vnd"] = format_vnd
templates.env.filters["dt"] = lambda ts: time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(ts)) if ts else ""

STATUS_LABELS = {"PENDING": "Chờ xử lý", "PROCESSING": "Đang xử lý", "DONE": "Hoàn thành", "FAILED": "Lỗi"}
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- auth (staff)

class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request, conn=Depends(get_conn)):
    token = request.cookies.get("session")
    if token:
        row = conn.execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                           "WHERE s.token = ? AND s.expires_at > ?", (token, time.time())).fetchone()
        if row:
            return row
    raise LoginRequired()


def load_customer_for(conn, user, customer_id: int):
    """Authorization: sales staff only see customers they own; admin sees everything."""
    c = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not c or (user["role"] != "admin" and c["owner_id"] != user["id"]):
        raise HTTPException(404, "Không tìm thấy khách hàng")
    return c


def load_quote_for(conn, user, quote_id: int):
    q = conn.execute("SELECT q.*, c.owner_id, c.name AS customer_name FROM quotes q "
                     "JOIN customers c ON c.id = q.customer_id WHERE q.id=?", (quote_id,)).fetchone()
    if not q or (user["role"] != "admin" and user["id"] not in (q["owner_id"], q["created_by"])):
        raise HTTPException(404, "Không tìm thấy báo giá")
    return q


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), conn=Depends(get_conn)):
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u or not db.verify_password(password, u["password_hash"]):
        log.info("login failed for %r", username)
        return templates.TemplateResponse(request, "login.html", {"error": "Sai tài khoản hoặc mật khẩu"},
                                          status_code=401)
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES (?,?,?)",
                 (token, u["id"], time.time() + SESSION_TTL))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=SESSION_TTL)
    return resp


@app.post("/logout")
def logout(request: Request, conn=Depends(get_conn)):
    conn.execute("DELETE FROM sessions WHERE token=?", (request.cookies.get("session", ""),))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ---------------------------------------------------------------- staff pages

def agent_status(conn):
    row = conn.execute("SELECT worker_id, last_seen FROM agents ORDER BY last_seen DESC LIMIT 1").fetchone()
    if not row:
        return {"online": False, "text": "Chưa từng kết nối"}
    age = time.time() - row["last_seen"]
    return {"online": age < 30, "text": f"{row['worker_id']} - lần cuối {int(age)} giây trước"}


@app.get("/", response_class=HTMLResponse)
def customers_list(request: Request, user=Depends(current_user), conn=Depends(get_conn)):
    if user["role"] == "admin":
        rows = conn.execute("SELECT * FROM customers ORDER BY code").fetchall()
    else:
        rows = conn.execute("SELECT * FROM customers WHERE owner_id=? ORDER BY code", (user["id"],)).fetchall()
    return templates.TemplateResponse(request, "customers.html",
                                      {"user": user, "customers": rows, "agent": agent_status(conn)})


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail(customer_id: int, request: Request, user=Depends(current_user), conn=Depends(get_conn)):
    c = load_customer_for(conn, user, customer_id)
    quotes = conn.execute("SELECT * FROM quotes WHERE customer_id=? ORDER BY created_at DESC", (customer_id,)).fetchall()
    quotes = [dict(q, total=json.loads(q["payload_json"])["total"]) for q in quotes]
    return templates.TemplateResponse(request, "customer.html",
                                      {"user": user, "c": c, "quotes": quotes, "agent": agent_status(conn)})


def _form_ctx(conn, user, c, idem_key, values=None, errors=None):
    products = conn.execute("SELECT * FROM products ORDER BY sku").fetchall()
    return {"user": user, "c": c, "products": products, "idem_key": idem_key, "rows": range(1, FORM_ROWS + 1),
            "v": values or {}, "errors": errors or [],
            "products_json": json.dumps({p["sku"]: dict(p) for p in products}, ensure_ascii=False)}


@app.get("/customers/{customer_id}/quotes/new", response_class=HTMLResponse)
def quote_form(customer_id: int, request: Request, user=Depends(current_user), conn=Depends(get_conn)):
    c = load_customer_for(conn, user, customer_id)
    # A fresh idempotency key is embedded in each rendered form: double-clicks / browser resubmits reuse it.
    return templates.TemplateResponse(request, "quote_form.html", _form_ctx(conn, user, c, str(uuid.uuid4())))


@app.post("/customers/{customer_id}/quotes")
async def quote_submit(customer_id: int, request: Request, user=Depends(current_user), conn=Depends(get_conn)):
    c = load_customer_for(conn, user, customer_id)
    form = await request.form()
    values = {k: str(v) for k, v in form.items()}
    idem_key = values.get("idempotency_key", "")
    try:
        uuid.UUID(idem_key)
    except ValueError:
        raise HTTPException(400, "Thiếu idempotency key")

    products = {p["sku"]: p for p in conn.execute("SELECT * FROM products").fetchall()}
    items = []
    for i in range(1, FORM_ROWS + 1):
        sku, name = values.get(f"sku_{i}", "").strip(), values.get(f"name_{i}", "").strip()
        qty, price = values.get(f"qty_{i}", "").strip(), values.get(f"price_{i}", "").strip()
        if not (sku or name or qty or price):
            continue  # empty row
        p = products.get(sku)
        items.append({"sku": sku, "name": name or (p["name"] if p else ""),
                      "spec": values.get(f"spec_{i}", ""), "unit": values.get(f"unit_{i}", ""),
                      "quantity": qty, "unit_price": price,
                      # list price comes from the DB, never from the browser
                      "list_price": p["list_price"] if p else None})
    raw = {"items": items, **{k: values.get(k, "") for k in
                              ("vat_rate", "payment_terms", "delivery_terms", "delivery_time", "validity_days", "notes")}}
    try:
        computed = validate_and_compute(raw)
    except QuoteValidationError as e:
        return templates.TemplateResponse(request, "quote_form.html",
                                          _form_ctx(conn, user, c, idem_key, values, e.errors), status_code=422)

    # Snapshot customer + salesperson data at submit time so later edits don't alter this quote.
    payload = {
        **computed,
        "customer": {k: c[k] for k in ("code", "name", "tax_code", "address", "contact_name", "phone", "email")},
        "sales": {"name": user["full_name"], "username": user["username"]},
    }
    quote_id, created = db.create_quote(conn, customer_id=c["id"], user_id=user["id"],
                                        idempotency_key=idem_key, payload=payload)
    log.info("quote %s %s by %s", quote_id, "created" if created else "duplicate submit ignored", user["username"])
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)  # POST-redirect-GET


@app.get("/quotes/{quote_id}", response_class=HTMLResponse)
def quote_detail(quote_id: int, request: Request, user=Depends(current_user), conn=Depends(get_conn)):
    q = load_quote_for(conn, user, quote_id)
    events = conn.execute("SELECT * FROM quote_events WHERE quote_id=? ORDER BY id", (quote_id,)).fetchall()
    return templates.TemplateResponse(request, "quote.html", {
        "user": user, "q": q, "p": json.loads(q["payload_json"]), "events": events, "agent": agent_status(conn),
        "review": json.loads(q["ai_review_json"]) if q["ai_review_json"] else None})


@app.get("/quotes/{quote_id}/status.json")
def quote_status(quote_id: int, user=Depends(current_user), conn=Depends(get_conn)):
    q = load_quote_for(conn, user, quote_id)
    return {"status": q["status"], "attempts": q["attempts"], "error": q["error"]}


@app.get("/quotes/{quote_id}/download")
def quote_download(quote_id: int, user=Depends(current_user), conn=Depends(get_conn)):
    q = load_quote_for(conn, user, quote_id)
    if q["status"] != "DONE" or not q["output_path"] or not os.path.exists(q["output_path"]):
        raise HTTPException(404, "File chưa sẵn sàng")
    log.info("download %s by %s", q["quote_no"], user["username"])
    return FileResponse(q["output_path"], filename=os.path.basename(q["output_path"]),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.post("/quotes/{quote_id}/retry")
def quote_retry(quote_id: int, user=Depends(current_user), conn=Depends(get_conn)):
    load_quote_for(conn, user, quote_id)
    db.manual_retry(conn, quote_id, user["username"])
    return RedirectResponse(f"/quotes/{quote_id}", status_code=303)


# ---------------------------------------------------------------- agent API (Mac mini)

def require_agent(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not AGENT_TOKEN or not secrets.compare_digest(token, AGENT_TOKEN):
        raise HTTPException(401, "invalid agent token")


class ClaimBody(BaseModel):
    worker_id: str


class ReportBody(BaseModel):
    worker_id: str
    attempt: int


class FailBody(ReportBody):
    error: str
    retryable: bool = True


@app.post("/api/agent/claim", dependencies=[Depends(require_agent)])
def agent_claim(body: ClaimBody, conn=Depends(get_conn)):
    job = db.claim_job(conn, body.worker_id)
    if not job:
        return Response(status_code=204)
    log.info("job %s claimed by %s (attempt %s)", job["quote_no"], body.worker_id, job["attempt"])
    return job


@app.post("/api/agent/jobs/{job_id}/heartbeat", dependencies=[Depends(require_agent)])
def agent_heartbeat(job_id: int, body: ReportBody, conn=Depends(get_conn)):
    if not db.extend_lease(conn, job_id, body.worker_id, body.attempt):
        raise HTTPException(409, "lease lost")
    return {"ok": True}


@app.post("/api/agent/jobs/{job_id}/complete", dependencies=[Depends(require_agent)])
async def agent_complete(job_id: int, worker_id: str = Form(...), attempt: int = Form(...), sha256: str = Form(...),
                         ai_review: str = Form(default=""), file: UploadFile = File(...), conn=Depends(get_conn)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise HTTPException(400, "checksum mismatch")  # agent will retry the upload
    row = conn.execute("SELECT quote_no FROM quotes WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    # Write to a temp file then rename: a crash never leaves a half-written quotation behind.
    final = os.path.join(STORAGE_DIR, f"{row['quote_no']}.docx")
    tmp = f"{final}.{attempt}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    review = json.loads(ai_review) if ai_review else None
    if not db.complete_job(conn, job_id, worker_id, attempt, final, sha256, review):
        os.remove(tmp)
        raise HTTPException(409, "lease lost - result discarded")
    os.replace(tmp, final)
    log.info("job %s completed by %s", row["quote_no"], worker_id)
    return {"ok": True}


@app.post("/api/agent/jobs/{job_id}/fail", dependencies=[Depends(require_agent)])
def agent_fail(job_id: int, body: FailBody, conn=Depends(get_conn)):
    status = db.fail_job(conn, job_id, body.worker_id, body.attempt, body.error[:1000], body.retryable)
    if status is None:
        raise HTTPException(409, "lease lost")
    log.warning("job %s failed (%s): %s", job_id, status, body.error[:200])
    return {"status": status}


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True})
