"""Deterministic business logic shared by the web app and the Mac mini agent.

Everything here is pure Python: no I/O, no AI. Money is handled as integer VND
and quantities as Decimal so the numbers on a quotation are reproducible and testable.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ALLOWED_VAT_RATES = (0, 5, 8, 10)
MAX_ITEMS = 20


class QuoteValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _to_decimal(value, field: str, errors: list[str]) -> Decimal | None:
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        errors.append(f"{field}: không phải số hợp lệ")
        return None
    if not d.is_finite():
        errors.append(f"{field}: không phải số hợp lệ")
        return None
    return d


def validate_and_compute(raw: dict) -> dict:
    """Validate a raw quote request and return a normalized payload with totals.

    Raises QuoteValidationError with a list of human readable (Vietnamese) messages.
    """
    errors: list[str] = []
    items_out = []

    raw_items = raw.get("items") or []
    if not raw_items:
        errors.append("Báo giá phải có ít nhất 1 dòng sản phẩm")
    if len(raw_items) > MAX_ITEMS:
        errors.append(f"Tối đa {MAX_ITEMS} dòng sản phẩm")

    for idx, it in enumerate(raw_items[:MAX_ITEMS], start=1):
        name = (it.get("name") or "").strip()
        if not name:
            errors.append(f"Dòng {idx}: thiếu tên sản phẩm")
        qty = _to_decimal(it.get("quantity"), f"Dòng {idx} số lượng", errors)
        price = _to_decimal(it.get("unit_price"), f"Dòng {idx} đơn giá", errors)
        if qty is not None and qty <= 0:
            errors.append(f"Dòng {idx}: số lượng phải > 0")
        if price is not None and (price < 0 or price != price.to_integral_value()):
            errors.append(f"Dòng {idx}: đơn giá phải là số nguyên VND >= 0")
        if qty is None or price is None:
            continue
        amount = int((qty * price).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        items_out.append({
            "no": idx,
            "sku": (it.get("sku") or "").strip(),
            "name": name,
            "spec": (it.get("spec") or "").strip(),
            "unit": (it.get("unit") or "").strip() or "kg",
            "quantity": format(qty.normalize(), "f"),  # plain notation: never "2E+3"
            "unit_price": int(price),
            "list_price": int(it["list_price"]) if it.get("list_price") not in (None, "") else None,
            "amount": amount,
        })

    try:
        vat_rate = int(raw.get("vat_rate", 10))
    except (TypeError, ValueError):
        vat_rate = -1
    if vat_rate not in ALLOWED_VAT_RATES:
        errors.append(f"Thuế VAT phải thuộc {ALLOWED_VAT_RATES}")

    try:
        validity_days = int(raw.get("validity_days", 15))
    except (TypeError, ValueError):
        validity_days = 0
    if not 1 <= validity_days <= 90:
        errors.append("Hiệu lực báo giá phải từ 1 đến 90 ngày")

    for field, label in (("payment_terms", "Điều kiện thanh toán"), ("delivery_terms", "Điều kiện giao hàng")):
        if not (raw.get(field) or "").strip():
            errors.append(f"Thiếu {label}")

    notes = (raw.get("notes") or "").strip()
    if len(notes) > 2000:
        errors.append("Ghi chú tối đa 2000 ký tự")

    if errors:
        raise QuoteValidationError(errors)

    subtotal = sum(i["amount"] for i in items_out)
    vat_amount = int((Decimal(subtotal) * vat_rate / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "items": items_out,
        "vat_rate": vat_rate,
        "subtotal": subtotal,
        "vat_amount": vat_amount,
        "total": subtotal + vat_amount,
        "payment_terms": raw["payment_terms"].strip(),
        "delivery_terms": raw["delivery_terms"].strip(),
        "delivery_time": (raw.get("delivery_time") or "").strip(),
        "validity_days": validity_days,
        "notes": notes,
    }


def format_vnd(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def format_qty(q: str) -> str:
    """Vietnamese notation: '2000' -> '2.000', '1500.5' -> '1.500,5'."""
    whole, _, frac = format(Decimal(q).normalize(), "f").partition(".")
    return format_vnd(int(whole)) + ("," + frac if frac else "")


_DIGITS = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]


def _read_triple(n: int, full: bool) -> str:
    hundreds, tens, ones = n // 100, (n // 10) % 10, n % 10
    words = []
    if full or hundreds:
        words += [_DIGITS[hundreds], "trăm"]
    if tens == 0:
        if ones and (full or hundreds):
            words.append("lẻ")
    elif tens == 1:
        words.append("mười")
    else:
        words += [_DIGITS[tens], "mươi"]
    if ones:
        if ones == 1 and tens > 1:
            words.append("mốt")
        elif ones == 5 and tens > 0:
            words.append("lăm")
        elif ones == 4 and tens > 1:
            words.append("tư")
        else:
            words.append(_DIGITS[ones])
    return " ".join(words)


def vnd_in_words(n: int) -> str:
    """Read an integer amount in Vietnamese, e.g. 1_250_000 -> 'Một triệu hai trăm năm mươi nghìn đồng'."""
    if n == 0:
        return "Không đồng"
    units = ["", "nghìn", "triệu", "tỷ", "nghìn tỷ", "triệu tỷ"]
    if n >= 1000 ** len(units):
        raise ValueError("amount too large")
    groups = []
    while n > 0:
        groups.append(n % 1000)
        n //= 1000
    parts = []
    for i in range(len(groups) - 1, -1, -1):
        if groups[i] == 0:
            continue
        text = _read_triple(groups[i], full=i != len(groups) - 1)
        parts.append(f"{text} {units[i]}".strip())
    s = " ".join(parts) + " đồng"
    return s[0].upper() + s[1:]
