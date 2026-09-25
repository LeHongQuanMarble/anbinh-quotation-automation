"""Scripted walk-through of the staff flow (login -> customer -> form -> submit), for demos/CI."""
import re
import sys

import requests

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
s = requests.Session()
s.post(B + "/login", data={"username": "sales1", "password": "sales123"}).raise_for_status()
form = s.get(B + "/customers/1/quotes/new").text
key = re.search(r'name="idempotency_key" value="([^"]+)"', form).group(1)
data = {
    "idempotency_key": key,
    "sku_1": "NaOH-99", "name_1": "Xút vảy NaOH", "spec_1": "Độ tinh khiết ≥ 99%, bao 25kg", "unit_1": "kg",
    "qty_1": "2000", "price_1": "14500",
    "sku_2": "PAC-31", "name_2": "Poly Aluminium Chloride (PAC)", "spec_2": "Al2O3 ≥ 31%, bao 25kg", "unit_2": "kg",
    "qty_2": "1500", "price_2": "9000",
    "sku_3": "", "name_3": "Chất trợ lắng A&B <đặc biệt>", "spec_3": "Can 20L", "unit_3": "can",
    "qty_3": "10", "price_3": "350000",
    "vat_rate": "8", "validity_days": "15",
    "payment_terms": "Chuyển khoản 50% khi đặt hàng, 50% còn lại trong 15 ngày sau khi giao",
    "delivery_terms": "Giao tại kho bên mua (TP.HCM), miễn phí vận chuyển",
    "delivery_time": "5 ngày làm việc", "notes": "Giá đã bao gồm bao bì tiêu chuẩn.",
}
r1 = s.post(B + "/customers/1/quotes", data=data)
r2 = s.post(B + "/customers/1/quotes", data=data)  # simulated double-click
print("first submit ->", r1.url)
print("second submit (same idempotency key) ->", r2.url)
