"""The Codex CLI step: an *advisory* review of the finished quotation.

Why advisory only: an LLM is good at reading a document "like a human" and noticing things that
look off (a price far below list, a quantity with a probable extra zero, notes that contradict the
payment terms). It is NOT a good fit for computing totals or deciding what goes in which cell -
that is done by deterministic code in renderer.py. So Codex never edits the file and never blocks
delivery; its output is stored as warnings the salesperson sees before sending the quote.

CODEX_MODE:
  off  - skip the step
  mock - rule-based stand-in with the same output shape (used in the demo / CI)
  real - run `codex exec` on the Mac mini (read-only sandbox, strict JSON answer, timeout)
If real mode fails for any reason we fall back to mock and say so: the quotation is still delivered.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("agent.codex")

PROMPT = """Bạn là trợ lý kiểm tra báo giá của công ty hóa chất An Bình Chemtech.
Dưới đây là dữ liệu báo giá (JSON) và nội dung text của file báo giá đã được tạo.
Hãy chỉ ra những điểm có thể bất thường mà nhân viên kinh doanh nên xem lại trước khi gửi khách, ví dụ:
đơn giá chênh lệch lớn so với giá niêm yết (list_price), số lượng bất thường, ghi chú mâu thuẫn với
điều kiện thanh toán/giao hàng, lỗi chính tả trong tên hàng, thông tin khách hàng thiếu.
KHÔNG tính lại tổng tiền, KHÔNG đề xuất nội dung mới cho báo giá.
Chỉ trả lời đúng một JSON object dạng: {{"warnings": ["...", "..."]}} (mảng rỗng nếu không có gì).

DỮ LIỆU:
{payload}

NỘI DUNG FILE:
{doc_text}
"""


def mock_review(payload: dict) -> dict:
    warnings = []
    for it in payload["items"]:
        lp = it.get("list_price")
        if lp:
            diff = (it["unit_price"] - lp) / lp * 100
            if abs(diff) >= 20:
                warnings.append(f"Dòng {it['no']} ({it['name']}): đơn giá {'thấp' if diff < 0 else 'cao'} hơn giá "
                                f"niêm yết {abs(diff):.0f}% - kiểm tra lại hoặc xin phê duyệt.")
        else:
            warnings.append(f"Dòng {it['no']} ({it['name']}): sản phẩm không có trong danh mục, không đối chiếu được giá.")
        if float(it["quantity"]) >= 100_000:
            warnings.append(f"Dòng {it['no']}: số lượng {it['quantity']} rất lớn - có thể nhập thừa số 0?")
    if payload["validity_days"] < 7:
        warnings.append("Hiệu lực báo giá dưới 7 ngày.")
    for k, label in (("tax_code", "mã số thuế"), ("email", "email")):
        if not payload["customer"].get(k):
            warnings.append(f"Khách hàng chưa có {label}.")
    return {"source": "mock (rule-based, giả lập Codex)", "warnings": warnings}


def codex_review(payload: dict, doc_text: str, timeout: int = 120) -> dict:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI not found on PATH")
    prompt = PROMPT.format(payload=json.dumps(payload, ensure_ascii=False, indent=1), doc_text=doc_text)
    with tempfile.TemporaryDirectory() as work:
        out_file = os.path.join(work, "answer.txt")
        # Read-only sandbox in an empty temp dir: Codex can't touch the file system or the quotation.
        subprocess.run(
            [codex, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--cd", work,
             "--output-last-message", out_file, "-"],
            input=prompt, text=True, capture_output=True, timeout=timeout, check=True)
        with open(out_file, encoding="utf-8") as f:
            answer = f.read()
    start, end = answer.find("{"), answer.rfind("}")
    data = json.loads(answer[start:end + 1])
    warnings = data.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise ValueError(f"unexpected codex output: {answer[:200]}")
    return {"source": "codex exec", "warnings": warnings[:20]}


def review(payload: dict, doc_text: str) -> dict | None:
    mode = os.environ.get("CODEX_MODE", "mock")
    if mode == "off":
        return None
    if mode == "real":
        try:
            return codex_review(payload, doc_text)
        except Exception as e:  # timeouts, not logged in, malformed JSON...
            log.warning("codex review failed, falling back to mock: %s", e)
            result = mock_review(payload)
            result["source"] += f" - Codex lỗi: {str(e)[:120]}"
            return result
    return mock_review(payload)
