# An Bình Chemtech – Prototype tạo báo giá tự động

Prototype cho luồng: **Trang khách hàng → "Tạo báo giá" → Nhập form → Gửi → Mac mini (agent + Codex CLI) → Template → File báo giá DOCX hoàn chỉnh**.

- Kiến trúc, lý do lựa chọn, xử lý rủi ro và bảo mật: xem **[docs/SOLUTION_PLAN.md](docs/SOLUTION_PLAN.md)**.
- File báo giá mẫu do prototype tạo ra: **[samples/BG-20260925-0001.docx](samples/)**.

## Chạy thử (local, khoảng 2 phút)

Yêu cầu: Python 3.10 trở lên. Chạy được trên macOS và Linux.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run_demo.sh            # chạy web app (port 8000) + agent "Mac mini" trong cùng một terminal
```

Mở http://127.0.0.1:8000 và đăng nhập **sales1 / sales123**. Tài khoản khác: sales2 / sales123, admin / admin123.

1. Chọn khách hàng **Công ty TNHH Sơn Phương Nam** rồi bấm **+ Tạo báo giá**.
2. Chọn mã sản phẩm (tên, quy cách, giá niêm yết được tự điền), nhập số lượng, kiểm tra điều kiện thanh toán và giao hàng, rồi bấm **Gửi**.
3. Trang trạng thái tự làm mới: *Chờ xử lý → Đang xử lý → Hoàn thành*. Bấm **Tải file báo giá (.docx)**.

Có thể chạy từng phần riêng (ví dụ để mô phỏng Mac mini mất kết nối):

```bash
.venv/bin/uvicorn app.main:app --port 8000        # terminal 1: web
.venv/bin/python -m agent.agent                    # terminal 2: agent (tắt đi → job nằm chờ, bật lại → tự xử lý)
SIMULATE_FAILURE=1 .venv/bin/python -m agent.agent # lần thử đầu của mỗi job bị crash → tự retry
.venv/bin/python scripts/demo_submit.py            # gửi báo giá bằng script (gửi 2 lần cùng key → chỉ 1 báo giá)
.venv/bin/pytest -q                                # 17 test: tính tiền, validate, render, hàng đợi, retry, lease, quyền
```

Cấu hình nằm trong `.env`. `run_demo.sh` tự tạo file này từ `.env.example` và sinh `AGENT_TOKEN` ngẫu nhiên.

## Cấu trúc

```
app/            Web "hệ thống quản lý nội bộ": FastAPI, HTML (Jinja2), SQLite
  main.py       Trang cho nhân viên + API cho agent (/api/agent/claim|heartbeat|complete|fail)
  db.py         Schema, dữ liệu giả, máy trạng thái hàng đợi (claim, lease, retry, fencing)
agent/          Chạy trên Mac mini
  agent.py      Vòng lặp poll → render → review → upload, heartbeat, backoff
  renderer.py   Copy template → điền (docxtpl) → kiểm tra file đầu ra (xác định, không dùng AI)
  codex_step.py Review tư vấn bằng `codex exec` (real) hoặc rule-based (mock)
common/         Logic dùng chung: validate, tính tiền, định dạng số, đọc số tiền bằng chữ
templates/      quotation_template.docx (mẫu báo giá có placeholder)
scripts/        make_template.py (tạo template giả), demo_submit.py
tests/          pytest
docs/           SOLUTION_PLAN.md
samples/        File báo giá mẫu đã tạo
```

## Đã hoàn thành

- Luồng chính end-to-end: đăng nhập → danh sách KH → chi tiết KH và lịch sử báo giá → form báo giá (tự điền sản phẩm, xem trước tổng tiền) → gửi → hàng đợi → agent xử lý → tải file DOCX.
- Trạng thái `PENDING / PROCESSING / DONE / FAILED`, nhật ký xử lý từng bước, hiển thị trạng thái online của Mac mini.
- Validate ở cả server và agent. Tiền tính bằng số nguyên VND/Decimal. Có số tiền bằng chữ tiếng Việt và ngày hết hiệu lực báo giá.
- Idempotency khi gửi trùng. Lease, heartbeat và tự đưa job về hàng đợi khi agent chết. Fencing chặn agent cũ ghi đè. Retry tối đa 3 lần, phân biệt lỗi tạm thời và lỗi vĩnh viễn. Nút "Chạy lại" cho job `FAILED`.
- Kiểm tra file đầu ra: không còn placeholder, đủ nội dung bắt buộc, SHA-256 được kiểm tra khi upload, ghi file tạm rồi rename (atomic).
- Phân quyền: nhân viên KD chỉ thấy KH và báo giá của mình. Agent xác thực bằng token riêng.

## Đang mock / chưa triển khai

| Phần | Trạng thái | Thay bằng thật như thế nào |
|---|---|---|
| Mac mini | Agent chạy cùng máy | Cài `agent/`, `common/`, `templates/` lên Mac mini, đặt `SERVER_URL` là URL HTTPS nội bộ, chạy bằng `launchd` (plist KeepAlive) |
| Codex CLI | `CODEX_MODE=mock` (rule-based, cùng định dạng output). Nhánh `real` đã viết nhưng **chưa chạy thử** vì máy làm bài không có Codex CLI | `npm i -g @openai/codex`, chạy `codex login`, đặt `CODEX_MODE=real`. Kiểm tra lại các flag của `codex exec` theo đúng phiên bản đã cài |
| Hệ thống quản lý nội bộ | Web tự dựng, dữ liệu giả | Tích hợp nút "Tạo báo giá" và API agent vào hệ thống thật |
| Template | Template giả sinh bằng script | Dùng template Word thật của công ty, thêm placeholder |
| Xuất PDF | Chưa làm (máy làm bài không có LibreOffice) | Thêm `soffice --headless --convert-to pdf` sau bước verify |
| Thông báo (email/Zalo) khi báo giá xong hoặc lỗi | Chưa làm | Gọi webhook khi chuyển sang `DONE` hoặc `FAILED` |

## Giả định

- Mỗi báo giá dùng một loại tiền (VND) và một mức VAT cho toàn bộ (0/5/8/10%). Đơn giá là số nguyên VND. Số lượng có thể là số thập phân.
- Nhân viên có thể sửa đơn giá khác giá niêm yết. Hệ thống không chặn, nhưng bước review sẽ cảnh báo khi lệch ≥ 20%.
- Đầu ra là DOCX, để nhân viên còn chỉnh sửa được trước khi gửi khách. File được lưu trên server, không lưu lâu dài trên Mac mini.
- Số lượng báo giá vừa phải (vài chục mỗi ngày) với 1–2 Mac mini, nên SQLite là đủ.
- Toàn bộ dữ liệu khách hàng, sản phẩm và giá đều là **giả lập**.

## Công cụ AI đã dùng

- **Claude Code (Anthropic)**: phân tích đề, đề xuất kiến trúc, viết code, test và tài liệu. Code được chạy và kiểm tra thực tế: toàn bộ test pass, và luồng demo được chạy end-to-end. Quá trình chạy thử đã phát hiện và sửa các lỗi thật: số lượng hiển thị dạng `2E+3` trong file (sau đó bổ sung bước verify số lượng), và lỗi SQLite dùng connection khác thread trong FastAPI.
- Trong chính giải pháp: **Codex CLI** được dùng làm bước review tư vấn (xem Solution Plan, mục 4).

## Nếu triển khai production

1. **Hạ tầng:** Postgres thay SQLite (`FOR UPDATE SKIP LOCKED` cho nhiều agent). Lưu file trên S3/MinIO hoặc NAS, có backup. Chạy HTTPS sau reverse proxy. Mac mini kết nối qua Tailscale/WireGuard.
2. **Bảo mật:** SSO công ty thay cho mật khẩu cục bộ. Thêm CSRF token cho form. Token riêng cho từng agent, có rotate. Mask dữ liệu KH trước khi gửi cho Codex, hoặc dùng tài khoản OpenAI doanh nghiệp đã được phê duyệt. Thêm rate limit cho đăng nhập.
3. **Vận hành:** agent chạy bằng `launchd`. Có cảnh báo khi agent offline quá N phút hoặc khi có job `FAILED`. Log tập trung. Có job dọn file tạm. Quản lý phiên bản template, và lưu phiên bản template đã dùng vào mỗi báo giá.
4. **Nghiệp vụ:** quy trình duyệt giá khi chiết khấu vượt ngưỡng. Xuất thêm PDF. Gửi email báo giá cho khách từ hệ thống. Đánh số báo giá theo quy tắc của công ty.
5. **Chất lượng:** CI chạy test. Có snapshot test cho file DOCX, và test tích hợp với Codex thật trên Mac mini.
