"""SQLite persistence: schema, seed data and the job-queue state machine.

Quote status lifecycle:

    PENDING --claim--> PROCESSING --complete--> DONE
       ^                   |  \\--fail(retryable, attempts left)--> PENDING
       |                   |  \\--fail(permanent or no attempts left)--> FAILED
       +--lease expired----+
    FAILED --manual retry--> PENDING
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "storage/app.db")
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "300"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('sales', 'admin')), password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, tax_code TEXT,
    address TEXT, contact_name TEXT, phone TEXT, email TEXT, owner_id INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, spec TEXT,
    unit TEXT NOT NULL, list_price INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    quote_no TEXT UNIQUE NOT NULL,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    created_by INTEGER NOT NULL REFERENCES users(id),
    idempotency_key TEXT UNIQUE NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'PROCESSING', 'DONE', 'FAILED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    worker_id TEXT,
    lease_until REAL,
    error TEXT,
    output_path TEXT,
    output_sha256 TEXT,
    ai_review_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quotes_status ON quotes(status, created_at);
CREATE TABLE IF NOT EXISTS quote_events (
    id INTEGER PRIMARY KEY, quote_id INTEGER NOT NULL REFERENCES quotes(id),
    at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    worker_id TEXT PRIMARY KEY, last_seen REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    # check_same_thread=False: FastAPI may resolve a dependency and run the endpoint on different threads;
    # each request still gets its own connection.
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so two agents can never claim the same job."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_password(password, salt), stored)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    # Demo data only - all fictional.
    users = [("sales1", "Nguyễn Văn An", "sales", "sales123"),
             ("sales2", "Trần Thị Bình", "sales", "sales123"),
             ("admin", "Quản trị viên", "admin", "admin123")]
    for u, n, r, p in users:
        conn.execute("INSERT INTO users(username, full_name, role, password_hash) VALUES (?,?,?,?)",
                     (u, n, r, hash_password(p)))
    customers = [
        ("KH001", "Công ty TNHH Sơn Phương Nam", "0312345678", "12 Nguyễn Văn Linh, Q.7, TP.HCM",
         "Lê Minh Tuấn", "0901 234 567", "tuan.le@sonphuongnam.example", 1),
        ("KH002", "Công ty CP Nhựa Hòa Phát Xanh", "0109876543", "KCN Quang Minh, Mê Linh, Hà Nội",
         "Phạm Thu Hà", "0987 654 321", "ha.pham@nhuaxanh.example", 1),
        ("KH003", "Công ty TNHH Dệt May Bình Dương", "3701122334", "KCN VSIP 1, Thuận An, Bình Dương",
         "Võ Quốc Huy", "0912 888 999", "huy.vo@detmaybd.example", 2),
    ]
    conn.executemany("INSERT INTO customers(code, name, tax_code, address, contact_name, phone, email, owner_id)"
                     " VALUES (?,?,?,?,?,?,?,?)", customers)
    products = [
        ("NaOH-99", "Xút vảy NaOH", "Độ tinh khiết ≥ 99%, bao 25kg", "kg", 14500),
        ("HCl-32", "Axit Clohydric HCl", "Nồng độ 32%, can 30kg", "kg", 4200),
        ("PAC-31", "Poly Aluminium Chloride (PAC)", "Al2O3 ≥ 31%, bao 25kg", "kg", 11800),
        ("H2O2-50", "Oxy già H2O2", "Nồng độ 50%, can 30kg", "kg", 9800),
        ("TiO2-R", "Titan Dioxit TiO2 Rutile", "Hàm lượng ≥ 93%, bao 25kg", "kg", 72000),
    ]
    conn.executemany("INSERT INTO products(sku, name, spec, unit, list_price) VALUES (?,?,?,?,?)", products)


def log_event(conn, quote_id: int, message: str, level: str = "INFO") -> None:
    conn.execute("INSERT INTO quote_events(quote_id, at, level, message) VALUES (?,?,?,?)",
                 (quote_id, time.time(), level, message))


def create_quote(conn, *, customer_id: int, user_id: int, idempotency_key: str, payload: dict) -> tuple[int, bool]:
    """Insert a PENDING quote. Returns (quote_id, created). Same idempotency key -> existing quote."""
    with tx(conn):
        row = conn.execute("SELECT id FROM quotes WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if row:
            return row["id"], False
        now = time.time()
        # Quote number: BG-YYYYMMDD-#### (sequence per day). Safe because we hold the write lock.
        day = time.strftime("%Y%m%d", time.localtime(now))
        n = conn.execute("SELECT COUNT(*) FROM quotes WHERE quote_no LIKE ?", (f"BG-{day}-%",)).fetchone()[0]
        quote_no = f"BG-{day}-{n + 1:04d}"
        payload = {**payload, "quote_no": quote_no, "quote_date": time.strftime("%d/%m/%Y", time.localtime(now))}
        cur = conn.execute(
            "INSERT INTO quotes(quote_no, customer_id, created_by, idempotency_key, payload_json, status,"
            " max_attempts, created_at, updated_at) VALUES (?,?,?,?,?, 'PENDING', ?, ?, ?)",
            (quote_no, customer_id, user_id, idempotency_key, json.dumps(payload, ensure_ascii=False),
             MAX_ATTEMPTS, now, now))
        log_event(conn, cur.lastrowid, "Tạo yêu cầu báo giá, chờ Mac mini xử lý")
        return cur.lastrowid, True


def requeue_expired(conn) -> None:
    """Jobs whose agent died mid-way (lease expired) go back to PENDING, or FAILED if out of attempts."""
    now = time.time()
    for row in conn.execute("SELECT id, attempts, max_attempts FROM quotes WHERE status='PROCESSING' AND lease_until < ?",
                            (now,)).fetchall():
        if row["attempts"] >= row["max_attempts"]:
            conn.execute("UPDATE quotes SET status='FAILED', error=?, worker_id=NULL, lease_until=NULL, updated_at=?"
                         " WHERE id=?", ("Hết thời gian xử lý (lease expired) và đã hết số lần thử", now, row["id"]))
            log_event(conn, row["id"], "Lease hết hạn, đã hết số lần thử -> FAILED", "ERROR")
        else:
            conn.execute("UPDATE quotes SET status='PENDING', worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                         (now, row["id"]))
            log_event(conn, row["id"], "Lease hết hạn (agent mất kết nối?) -> đưa lại vào hàng đợi", "WARN")


def claim_job(conn, worker_id: str):
    now = time.time()
    with tx(conn):
        conn.execute("INSERT INTO agents(worker_id, last_seen) VALUES (?, ?) "
                     "ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen", (worker_id, now))
        requeue_expired(conn)
        row = conn.execute("SELECT * FROM quotes WHERE status='PENDING' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        attempt = row["attempts"] + 1
        conn.execute("UPDATE quotes SET status='PROCESSING', attempts=?, worker_id=?, lease_until=?, error=NULL,"
                     " updated_at=? WHERE id=?", (attempt, worker_id, now + LEASE_SECONDS, now, row["id"]))
        log_event(conn, row["id"], f"{worker_id} nhận job (lần thử {attempt}/{row['max_attempts']})")
        return {"job_id": row["id"], "quote_no": row["quote_no"], "attempt": attempt,
                "lease_seconds": LEASE_SECONDS, "payload": json.loads(row["payload_json"])}


def _owned_processing(conn, job_id: int, worker_id: str, attempt: int):
    """Fencing check: only the agent holding the current lease/attempt may report on the job."""
    row = conn.execute("SELECT * FROM quotes WHERE id=?", (job_id,)).fetchone()
    if not row or row["status"] != "PROCESSING" or row["worker_id"] != worker_id or row["attempts"] != attempt:
        return None
    return row


def extend_lease(conn, job_id: int, worker_id: str, attempt: int) -> bool:
    with tx(conn):
        if not _owned_processing(conn, job_id, worker_id, attempt):
            return False
        conn.execute("UPDATE quotes SET lease_until=? WHERE id=?", (time.time() + LEASE_SECONDS, job_id))
        return True


def complete_job(conn, job_id: int, worker_id: str, attempt: int, output_path: str, sha256: str,
                 ai_review: dict | None) -> bool:
    with tx(conn):
        if not _owned_processing(conn, job_id, worker_id, attempt):
            return False
        conn.execute("UPDATE quotes SET status='DONE', output_path=?, output_sha256=?, ai_review_json=?,"
                     " lease_until=NULL, updated_at=? WHERE id=?",
                     (output_path, sha256, json.dumps(ai_review, ensure_ascii=False) if ai_review else None,
                      time.time(), job_id))
        log_event(conn, job_id, f"Hoàn thành, file đã upload (sha256 {sha256[:12]}…)")
        return True


def fail_job(conn, job_id: int, worker_id: str, attempt: int, error: str, retryable: bool) -> str | None:
    with tx(conn):
        row = _owned_processing(conn, job_id, worker_id, attempt)
        if not row:
            return None
        now = time.time()
        if retryable and row["attempts"] < row["max_attempts"]:
            status = "PENDING"
            log_event(conn, job_id, f"Lỗi tạm thời, sẽ thử lại: {error}", "WARN")
        else:
            status = "FAILED"
            log_event(conn, job_id, f"Thất bại: {error}", "ERROR")
        conn.execute("UPDATE quotes SET status=?, error=?, worker_id=NULL, lease_until=NULL, updated_at=? WHERE id=?",
                     (status, error, now, job_id))
        return status


def manual_retry(conn, quote_id: int, username: str) -> bool:
    with tx(conn):
        row = conn.execute("SELECT status, attempts FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not row or row["status"] != "FAILED":
            return False
        # Grant a fresh budget of attempts on top of what was already used.
        conn.execute("UPDATE quotes SET status='PENDING', error=NULL, max_attempts=?, updated_at=? WHERE id=?",
                     (row["attempts"] + MAX_ATTEMPTS, time.time(), quote_id))
        log_event(conn, quote_id, f"{username} yêu cầu chạy lại")
        return True
