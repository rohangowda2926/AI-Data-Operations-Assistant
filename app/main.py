"""Stateless sales analysis. No uploaded data is persisted or logged."""

import csv
import hmac
import io
import os
import re
import statistics
from collections import defaultdict
from datetime import date, datetime
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


MAX_BYTES = max(positive_setting("MAX_UPLOAD_BYTES", 10485760, 52428800), 10485760)
MAX_ROWS = positive_setting("MAX_ROWS", 10000, 100000)
API_KEY = os.getenv("API_KEY", "")
REQUIRED = {"order_id", "date", "product", "quantity", "unit_price"}
ALTERNATE_REQUIRED = {"Order ID", "Order Date", "Item Type", "Units Sold", "Unit Price"}
WALMART_REQUIRED = {"Store", "Date", "Weekly_Sales"}
app = FastAPI(title="AI Data Operations Assistant", version="1.0.0", docs_url="/api-docs",
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


@app.get("/docs", include_in_schema=False)
def friendly_docs():
    return RedirectResponse("/")


@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


def analyze_generic(text: str) -> dict:
    """Profile an arbitrary CSV without guessing domain-specific meaning."""
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    fields = reader.fieldnames
    if not fields or len(fields) != len(set(fields)) or any(not f.strip() for f in fields):
        raise HTTPException(422, "CSV must have unique, nonempty column names")
    rows = []
    for count, raw in enumerate(reader, 1):
        if count > MAX_ROWS:
            raise HTTPException(413, f"CSV exceeds {MAX_ROWS} data rows")
        if None in raw or any(v is None for v in raw.values()):
            raise HTTPException(422, f"Row {count + 1} has the wrong number of columns")
        rows.append({field: (raw[field] or "").strip() for field in fields})
    if not rows:
        raise HTTPException(422, "CSV must contain at least one data row")

    def number(value):
        try:
            if re.search(r"[A-Za-z]", value):
                return None
            cleaned = re.sub(r"[^0-9.+-]", "", value.replace(",", ""))
            if not cleaned or cleaned in {".", "+", "-"}:
                return None
            parsed = Decimal(cleaned)
            return float(parsed) if parsed.is_finite() else None
        except (InvalidOperation, AttributeError):
            return None

    def parsed_date(value):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(value, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    profiles = []
    for field in fields:
        values = [row[field] for row in rows]
        nonempty = [value for value in values if value]
        numeric = [number(value) for value in nonempty]
        dates = [parsed_date(value) for value in nonempty]
        if nonempty and sum(value is not None for value in numeric) / len(nonempty) >= .9:
            kind = "numeric"
            nums = [value for value in numeric if value is not None]
            ordered = sorted(nums)
            q1, q3 = ordered[len(ordered) // 4], ordered[(len(ordered) * 3) // 4]
            iqr = q3 - q1
            outliers = sum(value < q1 - 1.5 * iqr or value > q3 + 1.5 * iqr for value in nums) if iqr else 0
            detail = {"min": min(nums), "max": max(nums), "mean": round(sum(nums) / len(nums), 4), "median": ordered[len(ordered) // 2], "outlier_count": outliers}
        elif nonempty and sum(value is not None for value in dates) / len(nonempty) >= .9:
            kind = "date"
            normalized = sorted(value for value in dates if value is not None)
            detail = {"min": normalized[0], "max": normalized[-1]}
        else:
            kind = "text"
            counts = defaultdict(int)
            for value in nonempty:
                counts[value] += 1
            detail = {"unique_count": len(counts), "top_values": [{"value": k, "count": v} for k, v in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]]}
        profiles.append({"column": field, "type": kind, "missing_count": len(values) - len(nonempty), "unique_count": len(set(nonempty)), "details": detail})
    duplicate_rows = len(rows) - len({tuple(row[field] for field in fields) for row in rows})
    return {"input_format": "generic_csv", "summary": f"Profiled {len(rows)} rows across {len(fields)} columns without applying a domain-specific model.", "dataset_profile": {"row_count": len(rows), "column_count": len(fields), "duplicate_row_count": duplicate_rows, "columns": profiles}, "method": {"inference": "Numeric when at least 90% of nonempty values parse as finite numbers; date when at least 90% match common date formats; otherwise text.", "limitations": "Inferred types describe the file structure. They do not prove business meaning, causality, or data quality. Uploads are analyzed in memory and not persisted."}}


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
        if not fields or len(fields) != len(set(fields)):
            raise HTTPException(422, "CSV headers must be unique")
        source_format = "native"
        source_map = {field: field for field in REQUIRED} if set(fields) == REQUIRED else None
        if source_map is None and ALTERNATE_REQUIRED.issubset(set(fields)):
            source_map = {"order_id": "Order ID", "date": "Order Date", "product": "Item Type", "quantity": "Units Sold", "unit_price": "Unit Price"}
            source_format = "sales_export"
        if source_map is None and WALMART_REQUIRED.issubset(set(fields)):
            source_format = "walmart_weekly"
            source_map = {"order_id": "Store", "date": "Date", "product": "Store", "quantity": "Store", "unit_price": "Weekly_Sales"}
        if source_map is None:
            return analyze_generic(text)
        for count, row in enumerate(reader, 1):
            number = reader.line_num
            if count > MAX_ROWS:
                raise HTTPException(413, f"CSV exceeds {MAX_ROWS} data rows")
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("Row has the wrong number of columns")
                if source_format == "walmart_weekly":
                    store = str(row["Store"]).strip()
                    source_date = str(row["Date"]).strip()
                    row = {"order_id": f"{store}-{source_date}", "date": source_date,
                           "product": f"Store {store}", "quantity": "1",
                           "unit_price": str(row["Weekly_Sales"]).strip()}
                else:
                    row = {canonical: str(row[source]).strip() for canonical, source in source_map.items()}
                if any(not v for v in row.values()):
                    raise ValueError("All fields are required")
                if len(row["order_id"]) > 100 or len(row["product"]) > 200:
                    raise ValueError("order_id/product exceeds 100/200 characters")
                if row["order_id"] in seen:
                    raise ValueError("Duplicate order_id; use one row per order")
                try:
                    if source_format == "walmart_weekly":
                        row["date"] = datetime.strptime(row["date"], "%d-%m-%Y").date().isoformat()
                    elif source_map["date"] == "date":
                        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]):
                            raise ValueError
                        row["date"] = date.fromisoformat(row["date"]).isoformat()
                    else:
                        row["date"] = datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat()
                except ValueError:
                    raise ValueError("Date must use YYYY-MM-DD or M/D/YYYY for supported sales exports") from None
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
        "summary": (f"Analyzed {len(rows)} weekly store observations across {len(daily)} observed sales days; {len(anomalies)} observations flagged for review."
                     if source_format == "walmart_weekly" else
                     f"Analyzed {len(rows)} orders across {len(daily)} observed sales days; {len(anomalies)} orders flagged for review."),
        "input_format": source_format,
        "method": {"high_revenue": "At least 5 orders; revenue > median + 6 * median absolute deviation. When MAD=0: median + max(1, 2*median).",
                   "limitations": ("Each row is a store-week observation; quantity is set to 1 because this dataset has sales value but no unit count. Store observations are not individual orders. One currency per upload; flags are review prompts, not proof of errors. Missing calendar dates are not treated as zero sales."
                                  if source_format == "walmart_weekly" else
                                  "One row per order, one currency per upload; no tax, discounts, returns or currency conversion. Flags are review prompts, not proof of errors. Missing calendar dates are not treated as zero sales."),
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
