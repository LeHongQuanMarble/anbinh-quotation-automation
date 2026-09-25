import json
import os
import re
import time
import uuid

import pytest

from common.quote_logic import QuoteValidationError, format_qty, validate_and_compute, vnd_in_words

RAW = {
    "items": [
        {"sku": "NaOH-99", "name": "Xút vảy NaOH", "spec": "bao 25kg", "unit": "kg",
         "quantity": "2000", "unit_price": "14500", "list_price": 14500},
        {"sku": "", "name": "Hàng A&B <x>", "spec": "", "unit": "can", "quantity": "1.5", "unit_price": "333333"},
    ],
    "vat_rate": "8", "validity_days": "15", "payment_terms": "CK 30 ngày", "delivery_terms": "Tại kho", "notes": "",
}


def payload_for(raw=RAW):
    p = validate_and_compute(raw)
    return {**p, "quote_no": "BG-TEST-0001", "quote_date": "25/09/2026",
            "customer": {"code": "KH", "name": "Công ty A & B", "tax_code": "1", "address": "x",
                         "contact_name": "y", "phone": "z", "email": "e"},
            "sales": {"name": "NV", "username": "u"}}


# ---------------------------------------------------------------- business logic

def test_totals_and_rounding():
    p = validate_and_compute(RAW)
    assert [i["amount"] for i in p["items"]] == [29_000_000, 500_000]  # 1.5 * 333333 = 499999.5 -> 500000
    assert p["subtotal"] == 29_500_000 and p["vat_amount"] == 2_360_000 and p["total"] == 31_860_000
    assert p["items"][0]["quantity"] == "2000"  # never scientific notation


@pytest.mark.parametrize("patch,msg", [
    ({"items": []}, "ít nhất 1"),
    ({"vat_rate": "7"}, "VAT"),
    ({"payment_terms": " "}, "thanh toán"),
    ({"items": [{"name": "x", "quantity": "-1", "unit_price": "1"}]}, "số lượng"),
    ({"items": [{"name": "x", "quantity": "abc", "unit_price": "1"}]}, "không phải số"),
    ({"items": [{"name": "x", "quantity": "1", "unit_price": "1.5"}]}, "số nguyên"),
])
def test_validation_errors(patch, msg):
    with pytest.raises(QuoteValidationError) as e:
        validate_and_compute({**RAW, **patch})
    assert msg in str(e.value)


def test_number_words_and_format():
    assert vnd_in_words(31_860_000) == "Ba mươi mốt triệu tám trăm sáu mươi nghìn đồng"
    assert vnd_in_words(1_005) == "Một nghìn không trăm lẻ năm đồng"
    assert vnd_in_words(2_014_500_000) == "Hai tỷ không trăm mười bốn triệu năm trăm nghìn đồng"
    assert format_qty("1500.50") == "1.500,5"


# ---------------------------------------------------------------- renderer

def test_render_fills_template_and_escapes(tmp_path):
    from agent.renderer import docx_text, render_quote
    path, ctx = render_quote(payload_for(), str(tmp_path))
    text = docx_text(path)
    assert "BG-TEST-0001" in text and "Công ty A & B" in text and "Hàng A&B <x>" in text
    assert "31.860.000" in text and "2.000" in text and "1,5" in text
    assert "{{" not in text and "{%" not in text


def test_render_rejects_tampered_totals(tmp_path):
    from agent.renderer import PermanentError, render_quote
    p = payload_for()
    p["total"] += 1
    with pytest.raises(PermanentError):
        render_quote(p, str(tmp_path))


# ---------------------------------------------------------------- queue / web app

@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import db, main
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(main, "AGENT_TOKEN", "secret")
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.post("/login", data={"username": "sales1", "password": "sales123"})
        yield c


def submit(client, key=None, **over):
    data = {"idempotency_key": key or str(uuid.uuid4()), "sku_1": "NaOH-99", "name_1": "Xút vảy NaOH",
            "unit_1": "kg", "qty_1": "100", "price_1": "14500", "vat_rate": "10", "validity_days": "15",
            "payment_terms": "CK", "delivery_terms": "Kho", **over}
    return client.post("/customers/1/quotes", data=data, follow_redirects=False)


AUTH = {"Authorization": "Bearer secret"}


def test_idempotent_submit(client):
    key = str(uuid.uuid4())
    a, b = submit(client, key), submit(client, key)
    assert a.status_code == b.status_code == 303 and a.headers["location"] == b.headers["location"]


def test_invalid_submit_shows_errors(client):
    r = submit(client, qty_1="0")
    assert r.status_code == 422 and "số lượng phải" in r.text


def test_authorization(client):
    assert client.get("/customers/3").status_code == 404  # owned by sales2
    assert client.post("/api/agent/claim", json={"worker_id": "w"}).status_code == 401


def test_full_flow_with_retry(client, tmp_path):
    loc = submit(client).headers["location"]
    qid = int(loc.rsplit("/", 1)[1])
    job = client.post("/api/agent/claim", json={"worker_id": "w1"}, headers=AUTH).json()
    assert client.get(f"/quotes/{qid}/status.json").json()["status"] == "PROCESSING"
    # attempt 1 crashes -> back to queue
    r = client.post(f"/api/agent/jobs/{qid}/fail", headers=AUTH,
                    json={"worker_id": "w1", "attempt": job["attempt"], "error": "boom", "retryable": True})
    assert r.json()["status"] == "PENDING"
    job = client.post("/api/agent/claim", json={"worker_id": "w1"}, headers=AUTH).json()
    assert job["attempt"] == 2

    from agent.renderer import render_quote
    path, _ = render_quote(job["payload"], str(tmp_path / "out"))
    data = open(path, "rb").read()
    import hashlib
    form = {"worker_id": "w1", "attempt": "2", "sha256": hashlib.sha256(data).hexdigest(),
            "ai_review": json.dumps({"source": "t", "warnings": []})}
    bad = client.post(f"/api/agent/jobs/{qid}/complete", headers=AUTH,
                      data={**form, "sha256": "0" * 64}, files={"file": ("x.docx", data)})
    assert bad.status_code == 400  # corrupted upload rejected
    ok = client.post(f"/api/agent/jobs/{qid}/complete", headers=AUTH, data=form, files={"file": ("x.docx", data)})
    assert ok.status_code == 200
    assert client.get(f"/quotes/{qid}/status.json").json()["status"] == "DONE"
    dl = client.get(f"/quotes/{qid}/download")
    assert dl.status_code == 200 and dl.content == data


def test_lease_expiry_and_fencing(client, monkeypatch):
    from app import db
    submit(client)
    job = client.post("/api/agent/claim", json={"worker_id": "w1"}, headers=AUTH).json()
    # w1 goes silent; pretend its lease expired
    conn = db.connect()
    conn.execute("UPDATE quotes SET lease_until=? WHERE id=?", (time.time() - 1, job["job_id"]))
    job2 = client.post("/api/agent/claim", json={"worker_id": "w2"}, headers=AUTH).json()
    assert job2["job_id"] == job["job_id"] and job2["attempt"] == 2
    # the stale worker w1 comes back and tries to report: rejected
    r = client.post(f"/api/agent/jobs/{job['job_id']}/fail", headers=AUTH,
                    json={"worker_id": "w1", "attempt": 1, "error": "late", "retryable": False})
    assert r.status_code == 409


def test_permanent_failure_and_manual_retry(client):
    loc = submit(client).headers["location"]
    qid = int(loc.rsplit("/", 1)[1])
    job = client.post("/api/agent/claim", json={"worker_id": "w1"}, headers=AUTH).json()
    client.post(f"/api/agent/jobs/{qid}/fail", headers=AUTH,
                json={"worker_id": "w1", "attempt": job["attempt"], "error": "template broken", "retryable": False})
    assert client.get(f"/quotes/{qid}/status.json").json()["status"] == "FAILED"
    client.post(f"/quotes/{qid}/retry")
    assert client.get(f"/quotes/{qid}/status.json").json()["status"] == "PENDING"


def test_mock_review_flags_discount():
    from agent.codex_step import mock_review
    p = payload_for()
    p["items"][0]["unit_price"] = 10000  # 31% below list
    warnings = mock_review(p)["warnings"]
    assert any("thấp hơn giá niêm yết 31%" in w for w in warnings)
    assert any("không có trong danh mục" in w for w in warnings)
