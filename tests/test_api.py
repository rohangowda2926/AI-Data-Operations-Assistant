from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from app import main

client = TestClient(main.app)
HEADER = "order_id,date,product,quantity,unit_price\n"


def post(text):
    return client.post("/analyze", content=text, headers={"Content-Type": "text/csv"})


def test_health_and_docs():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/docs", follow_redirects=False).status_code == 307
    assert client.get("/api-docs").status_code == 200
    assert "/analyze" in client.get("/openapi.json").json()["paths"]


def test_sample_kpis_and_outlier():
    response = post(Path("sample_data/sales.csv").read_bytes())
    assert response.status_code == 200
    result = response.json()
    assert result["kpis"] == {"total_revenue": "1090.00", "order_count": 6,
                              "units_sold": 113, "average_order_value": "181.67",
                              "product_count": 2, "observed_sales_days": 5}
    assert result["anomaly_count"] == 1
    assert result["anomalies"][0]["order_id"] == "ORD-006"


def test_common_sales_export_format():
    csv = "Region,Item Type,Order Date,Order ID,Units Sold,Unit Price,Total Revenue\n"
    csv += "Europe,Office Supplies,5/2/2014,341417157,1779,651.21,1158502.59\n"
    response = post(csv)
    assert response.status_code == 200
    assert response.json()["kpis"]["total_revenue"] == "1158502.59"


def test_walmart_weekly_format():
    csv = "Store,Date,Weekly_Sales,Holiday_Flag,Temperature\n"
    csv += "1,05-02-2010,1643690.9,0,42.31\n2,12-02-2010,100.00,1,38.51\n"
    response = post(csv)
    assert response.status_code == 200
    result = response.json()
    assert result["input_format"] == "walmart_weekly"
    assert result["kpis"]["total_revenue"] == "1643790.90"
    assert result["kpis"]["product_count"] == 2


@pytest.mark.parametrize("row", [
    "a,2026-02-30,Pen,1,2", "a,20261001,Pen,1,2", "a,2026-10-01,Pen,-1,2",
    "a,2026-10-01,Pen,1.5,2", "a,2026-10-01,Pen,0,2", "a,2026-10-01,Pen,1,NaN",
    "a,2026-10-01,Pen,1,Infinity", "a,2026-10-01,Pen,1,-2",
    "a,2026-10-01,Pen,1,2.001", "a,2026-10-01,Pen,1,0e999999999",
    "a,2026-10-01,,1,2", "a,2026-10-01,Pen,1",
    "a,2026-10-01,Pen,1,2,extra", 'a,2026-10-01,"unfinished,1,2',
])
def test_invalid_rows(row):
    assert post(HEADER + row).status_code == 422


def test_duplicates_reject_entire_file():
    row = "a,2026-10-01,Pen,1,2\n"
    assert post(HEADER + row + row).status_code == 422


@pytest.mark.parametrize("data", [b"\xff", b"", HEADER.encode(), b"x,x\n1,2", b"\x00"])
def test_bad_documents(data):
    assert post(data).status_code == 422


def test_money_and_small_sample():
    result = post(HEADER + "a,2026-10-01,Pen,3,0.10").json()
    assert result["kpis"]["total_revenue"] == "0.30"
    assert result["anomalies"] == []


def test_zero_revenue():
    assert post(HEADER + "a,2026-10-01,Pen,1,0").json()["anomalies"][0]["type"] == "zero_revenue_order"


def test_limits(monkeypatch):
    monkeypatch.setattr(main, "MAX_BYTES", 5)
    assert post(HEADER).status_code == 413
    monkeypatch.setattr(main, "MAX_BYTES", 1000)
    monkeypatch.setattr(main, "MAX_ROWS", 1)
    assert post(HEADER + "a,2026-10-01,Pen,1,2\nb,2026-10-01,Pen,1,2").status_code == 413


def test_auth(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "test-secret")
    assert post(HEADER).status_code == 401
    assert client.get("/health").status_code == 200
    assert client.post("/analyze", content=HEADER + "a,2026-10-01,Pen,1,2",
                       headers={"Content-Type": "text/csv", "X-API-Key": "test-secret"}).status_code == 200


def test_content_type():
    assert client.post("/analyze", json={}).status_code == 415
