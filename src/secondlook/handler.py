"""AWS Lambda handler for the Second Look web service.

Implements routes for serving the HTML UI, judging purchases, creating PayPal
orders, and capturing payments. All I/O is performed with the standard library.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import decimal
import threading
import urllib.request
from typing import Dict, Any, List, Callable

from .crowd import judge_purchase, SecondLookError as CrowdError
from .llm import chat
from .paypal import PayPalSandbox, SecondLookError as PayPalError

def _cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type",
    }

def _validate_purchase(purchase: Dict[str, Any]) -> tuple[bool, str]:
    required = ["item", "price", "currency", "terms", "need"]
    for key in required:
        if key not in purchase:
            return False, f"missing field {key}"
        value = purchase[key]
        if not isinstance(value, str):
            return False, f"field {key} must be a string"
        if len(value) > 4000:
            return False, f"field {key} exceeds length limit"
    try:
        price = decimal.Decimal(purchase["price"])
        if price <= 0:
            return False, "price must be positive"
    except Exception:
        return False, "price must be a valid decimal"
    return True, ""

_PURCHASE_KEYS = ("item", "price", "currency", "terms", "need")
_TOKEN_TTL = 300  # seconds a clearance stays valid


def _token_key() -> bytes:
    seed = os.getenv("SECONDLOOK_TOKEN_SEED") or os.getenv("PAYPAL_SECRET") or ""
    return hashlib.sha256(("secondlook-clearance:" + seed).encode("utf-8")).digest()


def _canonical(purchase: Dict[str, Any]) -> str:
    return json.dumps({k: str(purchase.get(k, "")) for k in _PURCHASE_KEYS}, sort_keys=True, separators=(",", ":"))


def _sign_clearance(purchase: Dict[str, Any], now: float = None) -> str:
    """Return a clearance token (expiry.nonce.signature) bound to this exact purchase. Issued only on a unanimous clear."""
    exp = int((now if now is not None else time.time()) + _TOKEN_TTL)
    nonce = base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
    msg = (_canonical(purchase) + "|" + str(exp) + "|" + nonce).encode("utf-8")
    mac = hmac.new(_token_key(), msg, hashlib.sha256).digest()
    return str(exp) + "." + nonce + "." + base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def _verify_clearance(purchase: Dict[str, Any], token: Any, now: float = None):
    """Return the token's nonce for an unexpired token this server issued for this exact purchase, else None."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    exp_text, nonce, sig = token.split(".")
    if not exp_text.isdigit() or not nonce or int(exp_text) < (now if now is not None else time.time()):
        return None
    msg = (_canonical(purchase) + "|" + exp_text + "|" + nonce).encode("utf-8")
    good = base64.urlsafe_b64encode(hmac.new(_token_key(), msg, hashlib.sha256).digest()).decode("ascii").rstrip("=")
    return nonce if hmac.compare_digest(good, sig) else None


_OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Second Look", "version": "1.1.0",
             "description": "A pause before money moves. Three AI reviewers from three makers check a purchase for scam signals. "
                            "PayPal orders are created only with a clearance token issued after a unanimous clear."},
    "paths": {
        "/agent/check": {"post": {"operationId": "checkPurchase", "summary": "Ask the crowd whether a purchase may proceed",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PurchaseRequest"}}}},
            "responses": {"200": {"description": "Decision with reasons and quotes", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Decision"}}}}}}},
        "/order": {"post": {"operationId": "createOrder", "summary": "Create a PayPal order for a cleared purchase (clearance token required)",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "required": ["purchase", "clearance_token"],
                "properties": {"purchase": {"$ref": "#/components/schemas/Purchase"}, "clearance_token": {"type": "string"}}}}}},
            "responses": {"200": {"description": "PayPal order created"}, "409": {"description": "No valid clearance for this exact purchase"}}}},
        "/capture": {"post": {"operationId": "captureOrder", "summary": "Capture an approved PayPal order",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "required": ["order_id"], "properties": {"order_id": {"type": "string"}}}}}},
            "responses": {"200": {"description": "Capture result"}}}},
    },
    "components": {"schemas": {
        "Purchase": {"type": "object", "required": list(_PURCHASE_KEYS), "properties": {
            "item": {"type": "string"}, "price": {"type": "string"}, "currency": {"type": "string"},
            "need": {"type": "string", "description": "Why the buyer is making this purchase, in their own words"},
            "terms": {"type": "string", "description": "The seller's terms"}}},
        "PurchaseRequest": {"type": "object", "required": ["purchase"], "properties": {"purchase": {"$ref": "#/components/schemas/Purchase"}}},
        "Decision": {"type": "object", "properties": {
            "decision": {"type": "string", "enum": ["allow", "hold"]}, "clear_count": {"type": "integer"},
            "reviews": {"type": "array", "items": {"type": "object"}},
            "clearance_token": {"type": "string", "nullable": True, "description": "Present only on allow; valid 5 minutes for this exact purchase; one token maps to one PayPal order"}}},
    }},
}


_RESULTS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Second Look: blind test results</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/styles/ag-grid.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/styles/ag-theme-quartz.css">
<style>
body{font-family:Inter,system-ui,sans-serif;background:#f7f8fa;color:#111;margin:0;padding:24px}
.wrap{max-width:1200px;margin:0 auto}h1{font-size:26px;margin:0 0 6px}p{color:#555;margin:0 0 14px;line-height:1.5}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}.tile{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px 18px;min-width:170px}
.tile b{display:block;font-size:26px}.tile span{color:#666;font-size:13px}
.bar{display:flex;gap:8px;margin:10px 0}.bar button{border:1px solid #1a7fdb;background:#fff;color:#1a7fdb;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}
.bar button.on{background:#1a7fdb;color:#fff}#grid{height:620px;width:100%}a{color:#1a7fdb}
</style></head><body><div class="wrap">
<h1>Blind test: 61 purchases Second Look had never seen</h1>
<p>A different AI model wrote these purchases: 33 scams with ordinary-looking seller terms and 28 normal purchases, half of them chosen to look risky.
Every row is one run of the three reviewers. Sort, filter or search any column. <a href="/">Back to the demo</a> &middot;
<a href="https://github.com/DKJager/secondlook/tree/main/bench">Data and re-run scripts</a></p>
<div class="tiles" id="tiles"></div>
<div class="bar"><button data-f="all" class="on">All 61</button><button data-f="miss">Misses only</button><button data-f="scam">Scams</button><button data-f="normal">Normal purchases</button>
<input id="q" placeholder="Search..." style="flex:1;border:1px solid #cfd4da;border-radius:8px;padding:8px 12px"></div>
<div id="grid" class="ag-theme-quartz"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/dist/ag-grid-community.min.js"></script>
<script>
const rows = __ROWS__;
const scams = rows.filter(r => r.label === 'scam'), normal = rows.filter(r => r.label === 'normal');
const tile = (n, t) => '<div class="tile"><b>' + n + '</b><span>' + t + '</span></div>';
document.getElementById('tiles').innerHTML =
  tile(scams.filter(r => r.decision === 'hold').length + ' of ' + scams.length, 'scams stopped') +
  tile(normal.filter(r => r.decision === 'allow').length + ' of ' + normal.length, 'normal purchases approved') +
  tile(rows.filter(r => !r.correct).length, 'misses, all listed below');
let mode = 'all';
const api = agGrid.createGrid(document.getElementById('grid'), {
  rowData: rows,
  defaultColDef: { sortable: true, filter: true, resizable: true, wrapText: true, autoHeight: true },
  columnDefs: [
    { colId: 'result', headerName: 'Result', width: 110, valueGetter: p => p.data.correct ? 'Correct' : 'MISS',
      cellStyle: p => ({ color: p.value === 'Correct' ? '#1e8e3e' : '#d93025', fontWeight: 600 }) },
    { field: 'label', headerName: 'Truth', width: 100 },
    { field: 'decision', headerName: 'Second Look', width: 130,
      cellStyle: p => ({ color: p.value === 'hold' ? '#d93025' : '#1e8e3e', fontWeight: 600 }) },
    { field: 'clears', headerName: 'Clear votes', width: 120, valueFormatter: p => p.value + ' of 3' },
    { field: 'category', width: 170 },
    { field: 'item', width: 220 },
    { field: 'price', width: 110, valueFormatter: p => '$' + Number(p.value).toLocaleString() },
    { field: 'need', headerName: 'Buyer said', flex: 2, minWidth: 280 },
    { field: 'reasons', headerName: 'Hold reasons', flex: 2, minWidth: 280 }
  ],
  isExternalFilterPresent: () => mode !== 'all',
  doesExternalFilterPass: n => mode === 'miss' ? !n.data.correct : n.data.label === mode
});
document.querySelectorAll('.bar button').forEach(b => b.addEventListener('click', () => {
  mode = b.dataset.f; document.querySelectorAll('.bar button').forEach(x => x.classList.toggle('on', x === b)); api.onFilterChanged();
}));
document.getElementById('q').addEventListener('input', e => api.setGridOption('quickFilterText', e.target.value));
</script></body></html>"""


def _results_page() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blind_results.json")
    with open(path, encoding="utf-8") as fh:
        return _RESULTS_PAGE.replace("__ROWS__", fh.read())


def _build_ask_fns(models: List[str]) -> List[Callable]:
    base_url = os.getenv("SECONDLOOK_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
    api_key = os.getenv("SECONDLOOK_API_KEY")
    if not api_key:
        raise CrowdError("SECONDLOOK_API_KEY is missing from environment")
    max_tokens = int(os.getenv("SECONDLOOK_MAX_TOKENS", "600"))
    temperature = 0.1
    timeout = 40.0

    def _make_fn(model_id: str) -> Callable:
        def fn(messages: List[Dict]) -> str:
            try:
                response = chat(
                    base_url=base_url,
                    key=api_key,
                    model=model_id,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=timeout,
                )
                content = response["choices"][0]["message"]["content"]
                return content
            except Exception as exc:
                raise CrowdError(f"Model {model_id} call failed: {exc}")
        return fn

    return [_make_fn(m) for m in models]

def lambda_handler(event: Dict[str, Any], context: object) -> Dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method") or event.get("httpMethod")
    if not method:
        return {
            "statusCode": 400,
            "headers": _cors_headers(),
            "body": json.dumps({"error": "missing HTTP method"}),
        }
    method = method.upper()

    raw_path = event.get("rawPath") or event.get("path") or event.get("requestContext", {}).get("http", {}).get("path") or "/"
    path = raw_path.rstrip("/") if raw_path != "/" else "/"

    headers = _cors_headers()

    if method == "GET" and path == "/":
        html_body = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Second Look</title>
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #f8f9fa;
            color: #212529;
            line-height: 1.6;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 2rem 1rem;
        }
        .card {
            background: #ffffff;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.04);
            max-width: 760px;
            width: 100%;
            padding: 2.5rem 2rem;
            display: flex;
            flex-direction: column;
            gap: 1.75rem;
        }
        .header { border-bottom: 1px solid #e9ecef; padding-bottom: 1.25rem; }
        .header h1 { font-size: 1.75rem; font-weight: 700; letter-spacing: -0.02em; color: #111; }
        .header .tagline { font-size: 0.95rem; color: #6c757d; margin-top: 0.35rem; }

        .form-grid { display: grid; gap: 1.25rem; }
        .field { display: flex; flex-direction: column; gap: 0.35rem; }
        .field label { font-size: 0.9rem; font-weight: 600; color: #343a40; }
        .field input, .field select, .field textarea {
            width: 100%; padding: 0.7rem 0.85rem;
            border: 1px solid #dee2e6; border-radius: 8px;
            font-size: 0.95rem; font-family: inherit;
            background: #fff; transition: border-color 0.15s, box-shadow 0.15s;
        }
        .field input:focus, .field select:focus, .field textarea:focus {
            outline: none; border-color: #339af0; box-shadow: 0 0 0 3px rgba(51,154,240,0.12);
        }
        .field textarea { min-height: 90px; resize: vertical; }
        .price-currency-row { display: grid; grid-template-columns: 1fr 140px; gap: 0.75rem; }

        .button-row { display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; }
        button {
            display: inline-flex; align-items: center; justify-content: center;
            padding: 0.7rem 1.5rem; border-radius: 8px; font-size: 0.95rem;
            font-weight: 600; font-family: inherit; cursor: pointer;
            border: 1px solid transparent; transition: all 0.15s;
        }
        button:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-primary { background: #339af0; color: #fff; border-color: #339af0; }
        .btn-primary:hover:not(:disabled) { background: #1c7ed6; }
        .btn-outline {
            background: #fff; color: #339af0; border-color: #339af0;
        }
        .btn-outline:hover:not(:disabled) { background: #e7f5ff; }

        .loading-state {
            display: none; align-items: center; gap: 0.75rem;
            padding: 1rem; background: #f1f3f5; border-radius: 8px;
            color: #495057; font-weight: 500;
        }
        .loading-state.active { display: flex; }
        .loading-state .spinner {
            width: 20px; height: 20px; border: 2.5px solid #dee2e6;
            border-top-color: #339af0; border-radius: 50%;
            animation: spin 0.6s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        .results-section { display: none; flex-direction: column; gap: 1.25rem; }
        .results-section.active { display: flex; }

        .verdict-banner {
            padding: 0.9rem 1.25rem; border-radius: 8px;
            font-weight: 700; font-size: 1rem; text-align: center;
        }
        .verdict-banner.cleared { background: #d3f9d8; color: #2b8a3e; }
        .verdict-banner.held { background: #ffe3e3; color: #c92a2a; }

        .reviewer-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 1rem;
        }
        @media (max-width: 720px) { .reviewer-grid { grid-template-columns: 1fr; } }
        .reviewer-card {
            border: 1px solid #e9ecef; border-radius: 10px;
            padding: 1.25rem; display: flex; flex-direction: column; gap: 0.65rem;
        }
        .reviewer-card .reviewer-header { font-weight: 700; font-size: 0.95rem; color: #212529; }
        .reviewer-card .model-id { font-size: 0.78rem; color: #868e96; word-break: break-all; }
        .badge {
            display: inline-block; padding: 0.2rem 0.65rem; border-radius: 999px;
            font-size: 0.78rem; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.03em; align-self: flex-start;
        }
        .badge-clear { background: #d3f9d8; color: #2b8a3e; }
        .badge-hold { background: #ffe3e3; color: #c92a2a; }
        .reviewer-card .reason { font-size: 0.9rem; color: #495057; }
        .reviewer-card .quote {
            font-style: italic; font-size: 0.85rem; color: #6c757d;
            border-left: 3px solid #dee2e6; padding-left: 0.75rem;
        }

        .pay-section { display: flex; flex-direction: column; gap: 0.85rem; }
        .order-details {
            display: none; padding: 1rem; background: #f8f9fa;
            border-radius: 8px; border: 1px solid #e9ecef; flex-direction: column; gap: 0.4rem;
        }
        .order-details.active { display: flex; }
        .order-details .detail-row { display: flex; gap: 0.5rem; font-size: 0.9rem; }
        .order-details .detail-label { font-weight: 600; color: #343a40; }
        .order-details a { color: #339af0; font-weight: 500; text-decoration: none; }
        .order-details a:hover { text-decoration: underline; }
        .error-note { color: #c92a2a; font-size: 0.85rem; font-weight: 500; display: none; }
        .error-note.active { display: block; }

        footer { margin-top: 2rem; font-size: 0.82rem; color: #adb5bd; text-align: center; }
    </style>
</head>
<body>
    <div class="card">
        <div class="header">
            <h1>Second Look</h1>
            <p class="tagline">A pause before money moves. Three AI reviewers from three makers check the purchase for scam signals. One objection, quoted from what the buyer wrote, and PayPal never creates the order. <a href="/results" style="color:#1a7fdb">See every result from our blind test of 61 purchases</a>.</p>
        </div>

        <div class="form-grid">
            <div class="field">
                <label for="item">Item</label>
                <input type="text" id="item" placeholder="Describe the item">
            </div>
            <div class="price-currency-row">
                <div class="field">
                    <label for="price">Price</label>
                    <input type="text" id="price" placeholder="0.00">
                </div>
                <div class="field">
                    <label for="currency">Currency</label>
                    <select id="currency">
                        <option value="USD">USD</option>
                        <option value="EUR">EUR</option>
                        <option value="GBP">GBP</option>
                    </select>
                </div>
            </div>
            <div class="field">
                <label for="need">Why are you buying this? (in your own words)</label>
                <textarea id="need" placeholder="Describe your need..."></textarea>
            </div>
            <div class="field">
                <label for="terms">Merchant terms (return policy, warranty, etc.)</label>
                <textarea id="terms" placeholder="Paste the merchant's terms here..."></textarea>
            </div>
        </div>

        <div class="button-row">
            <button type="button" id="loadScam" class="btn-outline">Load scam example</button>
            <button type="button" id="loadExample" class="btn-outline">Load normal example</button>
            <button id="askCrowd" class="btn-primary">Ask the crowd</button>
        </div>

        <div id="loadingState" class="loading-state">
            <div class="spinner"></div>
            Asking three models...
        </div>

        <div id="resultsSection" class="results-section">
            <div id="banner" class="verdict-banner" style="display:none;"></div>
            <div class="reviewer-grid" id="verdictsContainer">
                <div class="reviewer-card" id="verdict0" style="display:none;">
                    <div class="reviewer-header">Reviewer 1</div>
                    <div class="model-id"></div>
                    <div class="badge"></div>
                    <div class="reason"></div>
                    <div class="quote"></div>
                </div>
                <div class="reviewer-card" id="verdict1" style="display:none;">
                    <div class="reviewer-header">Reviewer 2</div>
                    <div class="model-id"></div>
                    <div class="badge"></div>
                    <div class="reason"></div>
                    <div class="quote"></div>
                </div>
                <div class="reviewer-card" id="verdict2" style="display:none;">
                    <div class="reviewer-header">Reviewer 3</div>
                    <div class="model-id"></div>
                    <div class="badge"></div>
                    <div class="reason"></div>
                    <div class="quote"></div>
                </div>
            </div>
        </div>

        <div class="pay-section">
            <button id="confirmPay" disabled class="btn-primary">Confirm & Pay with PayPal (sandbox)</button>
            <div id="errorNote" class="error-note"></div>
            <div id="orderInfo" class="order-details">
                <div class="detail-row"><span class="detail-label">Order ID:</span> <span id="orderId"></span></div>
                <div class="detail-row"><span class="detail-label">Status:</span> <span id="orderStatus"></span></div>
                <div class="detail-row"><span class="detail-label">Approve:</span> <a id="approveUrl" target="_blank">Open PayPal sandbox</a></div>
                <button id="captureOrder" class="btn-primary" style="margin-top:0.25rem;">Capture</button>
            </div>
        </div>
    </div>

    <footer>
        <p>Second Look &mdash; open-source, Apache-2.0. Three independent open models; PayPal Orders v2 sandbox.</p>
    </footer>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const loadExampleBtn = document.getElementById('loadExample');
            const askCrowdBtn = document.getElementById('askCrowd');
            const confirmPayBtn = document.getElementById('confirmPay');
            const captureOrderBtn = document.getElementById('captureOrder');
            const loadingState = document.getElementById('loadingState');
            const resultsSection = document.getElementById('resultsSection');
            const errorNote = document.getElementById('errorNote');

            document.getElementById('loadScam').addEventListener('click', function() {
                document.getElementById('item').value = 'Gift cards x6';
                document.getElementById('price').value = '900.00';
                document.getElementById('currency').value = 'USD';
                document.getElementById('need').value = 'My grandson is in jail and needs bail tonight. The officer on the phone said to read him the card numbers.';
                document.getElementById('terms').value = 'Gift cards are non-refundable and cannot be exchanged for cash. Standard shipping 3-5 days.';
            });

            loadExampleBtn.addEventListener('click', function() {
                document.getElementById('item').value = 'Premium Wireless Headphones';
                document.getElementById('price').value = '129.99';
                document.getElementById('currency').value = 'USD';
                document.getElementById('need').value = 'Need high-quality headphones for daily commute and work calls';
                document.getElementById('terms').value = 'Limited Warranty: 1 year. Return Policy: 30-day money-back guarantee. Note: no returns on opened items. Shipping: free standard shipping.';
            });

            askCrowdBtn.addEventListener('click', async function() {
                const purchase = {
                    item: document.getElementById('item').value,
                    price: document.getElementById('price').value,
                    currency: document.getElementById('currency').value,
                    terms: document.getElementById('terms').value,
                    need: document.getElementById('need').value
                };

                errorNote.classList.remove('active');
                errorNote.textContent = '';
                resultsSection.classList.add('active');
                loadingState.classList.add('active');

                // hide previous results and payment info while loading
                document.getElementById('banner').style.display = 'none';
                for (let i = 0; i < 3; i++) {
                    document.getElementById('verdict' + i).style.display = 'none';
                }
                document.getElementById('orderInfo').classList.remove('active');
                confirmPayBtn.disabled = true;

                try {
                    const response = await fetch('/judge', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ purchase })
                    });

                    const result = await response.json();

                    if (!response.ok) {
                        errorNote.textContent = result.error || 'Request failed';
                        errorNote.classList.add('active');
                        loadingState.classList.remove('active');
                        return;
                    }

                    // Display verdicts
                    const verdicts = result.verdicts;
                    const models = result.models || [];

                    for (let i = 0; i < verdicts.length; i++) {
                        const verdict = verdicts[i];
                        const card = document.getElementById('verdict' + i);
                        const badge = card.querySelector('.badge');
                        const reason = card.querySelector('.reason');
                        const quote = card.querySelector('.quote');
                        const modelIdEl = card.querySelector('.model-id');

                        if (verdict.verdict === 'clear') {
                            badge.textContent = 'CLEAR';
                            badge.className = 'badge badge-clear';
                        } else {
                            badge.textContent = 'HOLD';
                            badge.className = 'badge badge-hold';
                        }
                        reason.textContent = verdict.reason || '';
                        quote.textContent = verdict.quote ? ((verdict.quote_from === 'need' ? 'Buyer said: ' : 'Seller terms: ') + '“' + verdict.quote + '”') : '';

                        if (models[i]) {
                            modelIdEl.textContent = models[i];
                        } else {
                            modelIdEl.textContent = '';
                        }

                        card.style.display = 'flex';
                    }

                    // Display banner
                    const banner = document.getElementById('banner');
                    if (result.cleared) {
                        banner.textContent = 'CLEARED by ' + result.clear_count + '/' + verdicts.length + ' reviewers';
                        banner.className = 'verdict-banner cleared';
                        confirmPayBtn.disabled = false;
                    } else {
                        banner.textContent = 'HELD: PayPal will not create this order';
                        if (result.summary) {
                            banner.textContent += ' \u2014 ' + result.summary;
                        }
                        banner.className = 'verdict-banner held';
                        confirmPayBtn.disabled = true;
                    }
                    banner.style.display = 'block';

                    // Store for later use
                    window.lastVerdict = result;
                } catch (err) {
                    errorNote.textContent = 'Network error: could not reach server.';
                    errorNote.classList.add('active');
                } finally {
                    loadingState.classList.remove('active');
                }
            });

            confirmPayBtn.addEventListener('click', async function() {
                const purchase = {
                    item: document.getElementById('item').value,
                    price: document.getElementById('price').value,
                    currency: document.getElementById('currency').value,
                    terms: document.getElementById('terms').value,
                    need: document.getElementById('need').value
                };

                errorNote.classList.remove('active');
                errorNote.textContent = '';

                try {
                    const response = await fetch('/order', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ purchase, verdict: window.lastVerdict })
                    });

                    const result = await response.json();

                    if (response.status === 200) {
                        document.getElementById('orderId').textContent = result.order_id;
                        document.getElementById('orderStatus').textContent = result.status;
                        document.getElementById('approveUrl').href = result.approve_url;
                        document.getElementById('approveUrl').textContent = 'Approve in PayPal sandbox';
                        document.getElementById('orderInfo').classList.add('active');
                        document.getElementById('captureOrder').dataset.orderId = result.order_id;
                    } else {
                        errorNote.textContent = result.error || 'Unknown error';
                        errorNote.classList.add('active');
                    }
                } catch (err) {
                    errorNote.textContent = 'Network error: could not reach server.';
                    errorNote.classList.add('active');
                }
            });

            captureOrderBtn.addEventListener('click', async function() {
                const orderId = this.dataset.orderId;
                errorNote.classList.remove('active');
                errorNote.textContent = '';

                try {
                    const response = await fetch('/capture', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ order_id: orderId })
                    });

                    const result = await response.json();
                    if (response.status === 200) {
                        document.getElementById('orderStatus').textContent = result.status || 'CAPTURED';
                    } else {
                        errorNote.textContent = result.error || 'Unknown error';
                        errorNote.classList.add('active');
                    }
                } catch (err) {
                    errorNote.textContent = 'Network error: could not reach server.';
                    errorNote.classList.add('active');
                }
            });
        });
    </script>
</body>
</html>"""
        return {
            "statusCode": 200,
            "headers": {**headers, "Content-Type": "text/html"},
            "body": html_body,
        }

    def _json_response(status: int, payload: dict) -> dict:
        return {
            "statusCode": status,
            "headers": {**headers, "Content-Type": "application/json"},
            "body": json.dumps(payload),
        }

    raw_body = event.get("body") or ""
    try:
        body = json.loads(raw_body) if raw_body else {}
    except Exception:
        return _json_response(400, {"error": "invalid JSON body"})

    if method == "GET" and path == "/results":
        return {"statusCode": 200, "headers": {"Content-Type": "text/html; charset=utf-8"}, "body": _results_page()}

    if method == "GET" and path == "/openapi.json":
        return _json_response(200, _OPENAPI)

    if method == "POST" and path in ("/judge", "/agent/check"):
        purchase = body.get("purchase")
        if not isinstance(purchase, dict):
            return _json_response(400, {"error": "missing purchase object"})
        valid, msg = _validate_purchase(purchase)
        if not valid:
            return _json_response(400, {"error": msg})

        if not os.getenv("SECONDLOOK_API_KEY"):
            return _json_response(503, {"error": "model API key missing"})

        models_env = os.getenv(
            "SECONDLOOK_MODELS",
            "nvidia/nemotron-3-super-120b-a12b,Qwen/Qwen3-30B-A3B-Instruct-2507,google/gemma-3-27b-it",
        )
        models = [m.strip() for m in models_env.split(",") if m.strip()]
        if not models:
            return _json_response(503, {"error": "no models configured"})

        ask_fns = _build_ask_fns(models)

        try:
            result = judge_purchase(purchase, ask_fns, min_clear=int(os.getenv("SECONDLOOK_MIN_CLEAR", "3")))
        except CrowdError as e:
            return _json_response(500, {"error": str(e)})

        # Ensure models are present in the response for the UI to display
        if "models" not in result:
            result["models"] = models
        result["token"] = _sign_clearance(purchase) if result.get("cleared") else None

        if path == "/agent/check":
            return _json_response(200, {
                "decision": "allow" if result.get("cleared") else "hold",
                "clear_count": result.get("clear_count"),
                "reviews": [{"model": models[v["model_index"]] if v["model_index"] < len(models) else "",
                             "verdict": v["verdict"], "reason": v["reason"], "quote": v["quote"],
                             "quote_from": v.get("quote_from")} for v in result.get("verdicts", [])],
                "clearance_token": result["token"],
                "next": "POST /order with {purchase, clearance_token}" if result.get("cleared") else "Do not pay. Show the reasons to the buyer.",
            })
        return _json_response(200, result)

    if method == "POST" and path == "/order":
        purchase = body.get("purchase")
        verdict = body.get("verdict") if isinstance(body.get("verdict"), dict) else {}
        if not isinstance(purchase, dict):
            return _json_response(400, {"error": "missing purchase"})
        valid, msg = _validate_purchase(purchase)
        if not valid:
            return _json_response(400, {"error": msg})
        token = body.get("clearance_token") or verdict.get("token")
        # The client's word is never trusted: only a clearance this server signed for this exact purchase creates an order.
        nonce = _verify_clearance(purchase, token)
        if not nonce:
            return _json_response(409, {"error": "no valid crowd clearance for this exact purchase"})

        if not os.getenv("PAYPAL_CLIENT_ID") or not os.getenv("PAYPAL_SECRET"):
            return _json_response(503, {"error": "PayPal credentials missing"})
        if not os.getenv("SECONDLOOK_API_KEY"):
            return _json_response(503, {"error": "model API key missing"})

        try:
            sandbox = PayPalSandbox(
                client_id=os.getenv("PAYPAL_CLIENT_ID"),
                secret=os.getenv("PAYPAL_SECRET"),
                base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
            )
            order = sandbox.create_order(purchase, "Second Look: cleared by 3 of 3 reviewers", request_id="secondlook-" + nonce)
        except PayPalError as e:
            return _json_response(502, {"error": str(e)})
        response_body = {
            "order_id": order.get("id"),
            "status": order.get("status"),
            "approve_url": order.get("approve_url"),
            "verdict_summary": "Second Look: cleared by 3 of 3 reviewers",
        }
        return _json_response(200, response_body)

    if method == "POST" and path == "/capture":
        order_id = body.get("order_id")
        if not isinstance(order_id, str) or not order_id.strip():
            return _json_response(400, {"error": "missing or invalid order_id"})
        if not os.getenv("PAYPAL_CLIENT_ID") or not os.getenv("PAYPAL_SECRET"):
            return _json_response(503, {"error": "PayPal credentials missing"})
        try:
            sandbox = PayPalSandbox(
                client_id=os.getenv("PAYPAL_CLIENT_ID"),
                secret=os.getenv("PAYPAL_SECRET"),
                base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
            )
            result = sandbox.capture_order(order_id)
        except PayPalError as e:
            return _json_response(502, {"error": str(e)})
        return _json_response(200, result)

    return _json_response(404, {"error": "not found"})