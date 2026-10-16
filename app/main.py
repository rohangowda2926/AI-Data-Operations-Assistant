"""Stateless sales analysis. No uploaded data is persisted or logged."""

import csv
import hmac
import io
import os
import re
import statistics
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool


def positive_setting(name: str, default: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


MAX_BYTES = positive_setting("MAX_UPLOAD_BYTES", 1048576, 10485760)
MAX_ROWS = positive_setting("MAX_ROWS", 10000, 100000)
API_KEY = os.getenv("API_KEY", "")
REQUIRED = {"order_id", "date", "product", "quantity", "unit_price"}
app = FastAPI(title="AI Data Operations Assistant", version="1.0.0",
              description="CSV sales validation, KPIs and explainable anomaly detection. "
              "Rule-based MVP; no LLM or external data services.")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse("app/static/index.html")


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


def analyze(data: bytes) -> dict:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "CSV must use UTF-8 encoding") from None
    if "\x00" in text:
        raise HTTPException(422, "CSV contains null bytes")
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    rows, errors, seen = [], [], set()
    try:
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)) or set(fields) != REQUIRED:
            raise HTTPException(422, "Headers must be exactly: order_id,date,product,quantity,unit_price")
        for count, row in enumerate(reader, 1):
            number = reader.line_num
            if count > MAX_ROWS:
                raise HTTPException(413, f"CSV exceeds {MAX_ROWS} data rows")
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("Row has the wrong number of columns")
                row = {k: v.strip() for k, v in row.items()}
                if any(not v for v in row.values()):
                    raise ValueError("All fields are required")
                if len(row["order_id"]) > 100 or len(row["product"]) > 200:
                    raise ValueError("order_id/product exceeds 100/200 characters")
                if row["order_id"] in seen:
                    raise ValueError("Duplicate order_id; use one row per order")
                if date.fromisoformat(row["date"]).isoformat() != row["date"]:
                    raise ValueError("Date must use YYYY-MM-DD")
                if not row["quantity"].isascii() or not row["quantity"].isdigit():
                    raise ValueError("Quantity must be a positive integer")
                quantity = int(row["quantity"])
                if not re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,2})?", row["unit_price"]):
                    raise ValueError("Price must be an ordinary decimal with at most two places")
                price = Decimal(row["unit_price"])
                if not 1 <= quantity <= 1000000:
                    raise ValueError("Quantity must be between 1 and 1000000")
                if not price.is_finite() or not 0 <= price <= Decimal("1000000000"):
                    raise ValueError("Unit price must be finite and between 0 and 1000000000")
                if price.as_tuple().exponent < -2:
                    raise ValueError("Unit price must have at most two decimal places")
                seen.add(row["order_id"])
                rows.append({**row, "quantity": quantity, "revenue": price * quantity, "row": number})
            except (ValueError, InvalidOperation):
                errors.append({"row": number, "message": "Invalid order: check unique ID, YYYY-MM-DD date, nonempty fields, positive integer quantity (max 1000000), and nonnegative price (max 1000000000, two decimals)"})
                if len(errors) >= 50:
                    break
    except (csv.Error, OverflowError):
        raise HTTPException(422, "Malformed CSV or field exceeds parser limits") from None
    if errors:
        raise HTTPException(422, {"message": "Entire upload rejected; no partial KPIs", "errors": errors, "error_limit": 50})
    if not rows:
        raise HTTPException(422, "CSV must contain at least one order")

    daily, products = defaultdict(Decimal), defaultdict(Decimal)
    for row in rows:
        daily[row["date"]] += row["revenue"]
        products[row["product"]] += row["revenue"]
    revenue = sum(daily.values(), Decimal(0))
    values = [r["revenue"] for r in rows]
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values])
    threshold = median + (Decimal(6) * mad if mad else max(Decimal(1), median * 2))
    anomalies = []
    for row in rows:
        number = row["row"]
        if len(rows) >= 5 and row["revenue"] > threshold:
            anomalies.append({"type": "high_order_revenue", "row": number,
                              "order_id": row["order_id"], "revenue": str(row["revenue"]),
                              "threshold": str(threshold)})
        if row["revenue"] == 0:
            anomalies.append({"type": "zero_revenue_order", "row": number, "order_id": row["order_id"]})
    money = lambda v: format(v.quantize(Decimal("0.01")), "f")
    return {
        "kpis": {"total_revenue": money(revenue), "order_count": len(rows),
                 "units_sold": sum(r["quantity"] for r in rows),
                 "average_order_value": money(revenue / len(rows)),
                 "product_count": len(products), "observed_sales_days": len(daily)},
        "daily_revenue": [{"date": k, "revenue": money(v)} for k, v in sorted(daily.items())],
        "revenue_by_product": [{"product": k, "revenue": money(v)} for k, v in sorted(products.items(), key=lambda x: (-x[1], x[0]))],
        "anomaly_count": len(anomalies), "anomalies": anomalies,
        "summary": f"Analyzed {len(rows)} orders across {len(daily)} observed sales days; {len(anomalies)} orders flagged for review.",
        "method": {"high_revenue": "At least 5 orders; revenue > median + 6 * median absolute deviation. When MAD=0: median + max(1, 2*median).",
                   "limitations": "One row per order, one currency per upload; no tax, discounts, returns or currency conversion. Flags are review prompts, not proof of errors. Missing calendar dates are not treated as zero sales.",
                   "money": "Decimal strings; averages rounded to two decimals using half-even rounding."},
    }


@app.post("/analyze", openapi_extra={"requestBody": {"required": True, "content": {
    "text/csv": {"schema": {"type": "string"}, "example": "order_id,date,product,quantity,unit_price\nA1,2026-10-01,Notebook,2,10.00\n"}}}})
async def analyze_csv(request: Request, x_api_key: str = Header(default="", description="Required only when API_KEY is configured on the server")):
    if API_KEY and not hmac.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(401, "Valid X-API-Key required")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() not in {"text/csv", "application/csv"}:
        raise HTTPException(415, "Send raw CSV with Content-Type: text/csv")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, f"Upload exceeds {MAX_BYTES} bytes")
        body.extend(chunk)
    return await run_in_threadpool(analyze, bytes(body))
