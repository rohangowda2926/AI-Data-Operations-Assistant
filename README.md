# AI Data Operations Assistant

A portfolio FastAPI service that turns sales CSVs into validated business KPIs and explainable review flags. This MVP uses deterministic statistics, not an LLM. No paid API, database or AI key is needed.

[Deploy this repository to Render](https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2Frohangowda2926%2FAI-Data-Operations-Assistant)

Live demo: https://ai-data-operations-assistant.onrender.com/  
API docs: https://ai-data-operations-assistant.onrender.com/docs  
Health: https://ai-data-operations-assistant.onrender.com/health

Verification: 27 tests passed; the live dashboard, API docs, health endpoint, and sample analysis request all return successfully. Sample revenue is 1090.00 with one high-order review flag.

## Run locally

Use Python 3.12. From this folder:

```sh
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The public dashboard is the recommended way to use the project: https://ai-data-operations-assistant.onrender.com/

The URL `http://127.0.0.1:8000` is local-only. It works only after you start Uvicorn on your own computer. `/` opens the SignalDesk dashboard; `/docs` is the developer Swagger page for testing the raw API; `/health` is the service check. The Swagger page is expected to show the `/health` and `/analyze` endpoints.

For local API testing, open http://127.0.0.1:8000/docs and use POST /analyze → Try it out. Paste CSV into the text/csv body. GET /health returns status and version.

Open http://127.0.0.1:8000/ for the user-facing SignalDesk dashboard. It accepts drag-and-drop CSV uploads, includes the sample dataset, and renders KPIs, product bars, and the review queue. Revenue is shown without a currency symbol because the CSV contract assumes one consistent currency per upload but does not prescribe which currency.

```sh
curl -X POST http://127.0.0.1:8000/analyze -H "Content-Type: text/csv" --data-binary @sample_data/sales.csv
```

In Windows PowerShell use `curl.exe` for this command.

## CSV contract

Headers must be exactly `order_id,date,product,quantity,unit_price` (any column order). UTF-8/BOM accepted; quoted fields supported. One row per unique order, one currency per file. Dates use YYYY-MM-DD. Quantity is a positive integer up to 1,000,000. Prices are finite, nonnegative, at most 1,000,000,000 and two decimal places. Returns, discounts, taxes and mixed currencies are outside this MVP.

Invalid uploads are rejected as a whole with HTTP 422, with up to 50 row errors. HTTP 413 means byte/row limit exceeded; 415 means wrong content type; 401 means API key missing/incorrect when configured. Defaults: 1 MiB and 10,000 orders. Empty CSVs, duplicate IDs/headers and malformed rows are rejected.

## Results and interpretation

- Total revenue = sum(quantity × unit_price); order count = valid unique rows.
- Units sold, average order value, product count and observed sales days.
- Revenue by product and by observed date. Absent dates are not assumed to have zero sales.
- High-order flag: with at least five orders, revenue exceeds median + 6 × median absolute deviation. If MAD is zero, threshold = median + max(1, 2 × median).
- Zero revenue orders are flagged separately. Flags require human review; they do not establish fraud or error.

Money is calculated with Decimal and returned as strings with two decimals (half-even rounding for averages). Synthetic sample: revenue 1090.00, 6 orders, 113 units, average order value 181.67, one unusually large order (ORD-006).

## Configuration and data handling

`.env.example` documents optional settings. The app reads process environment variables; it does not load `.env` automatically. Never commit secrets. Set `API_KEY` through your host's environment settings if protecting analysis, then send `X-API-Key` on every POST. Health and docs remain public. Leave the key unset only for a synthetic-data demo. No CORS access is enabled by default.

CSV data is held in memory for the request and is not stored or sent to third parties. Request bodies and credentials are not logged by application code. Hosting access logs may record request metadata. Do not upload confidential sales/customer information to the public demo. Limits bound each request; this MVP has no distributed rate limiting or durable audit storage. Add those before a real production rollout. Invalid numeric environment settings fail startup rather than silently disabling limits.

## Deploy to Render

The current deployment is live at https://ai-data-operations-assistant.onrender.com/ and is configured from `render.yaml`.

1. Push this folder as the root of a dedicated GitHub repository (do not publish the surrounding CV workspace or `sources/`).
2. Sign into Render and authorize GitHub access to only that repository.
3. Choose New → Blueprint and select the repository. The included `render.yaml` specifies the free Python web service, health check, build command and start command. Confirm free plan availability in your account before creation. Alternatively create a Web Service with build `pip install -r requirements.txt`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health path `/health`, Python 3.12.10.
4. Optional: set `API_KEY` in Render → Environment for private use. No other secret is required. Keep the request limits at their defaults unless necessary.
5. Wait for a successful deploy and copy the actual Render service URL.
6. Verify GET `/health` returns HTTP 200 with `status: ok`, `/docs` loads, and POST the sample CSV to `/analyze`. Expect the sample results above. Check an invalid CSV returns 422. Only then call the app live.

Official deployment reference: https://render.com/docs/deploy-fastapi

GitHub Actions runs validation tests on pushes and pull requests. The same Render service provides the SignalDesk dashboard at `/`, interactive Swagger API docs at `/docs`, and the JSON API at `/analyze`; a separate Vercel frontend is unnecessary.
