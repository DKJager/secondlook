"""AWS Lambda handler for the Second Look web service.

Implements routes for serving the HTML UI, judging purchases, creating PayPal
orders, and capturing payments. All I/O is performed with the standard library.
"""

import json
import os
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
            <p class="tagline">Three independent AI models review your purchase before PayPal creates the order.</p>
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
                <label for="need">What do you need it for?</label>
                <textarea id="need" placeholder="Describe your need..."></textarea>
            </div>
            <div class="field">
                <label for="terms">Merchant terms (return policy, warranty, etc.)</label>
                <textarea id="terms" placeholder="Paste the merchant's terms here..."></textarea>
            </div>
        </div>

        <div class="button-row">
            <button type="button" id="loadExample" class="btn-outline">Load example</button>
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
                        quote.textContent = verdict.quote || '';

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

    if method == "POST" and path == "/judge":
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

        return _json_response(200, result)

    if method == "POST" and path == "/order":
        purchase = body.get("purchase")
        verdict = body.get("verdict")
        if not isinstance(purchase, dict) or not isinstance(verdict, dict):
            return _json_response(400, {"error": "missing purchase or verdict"})
        valid, msg = _validate_purchase(purchase)
        if not valid:
            return _json_response(400, {"error": msg})
        if not isinstance(verdict.get("cleared"), bool):
            return _json_response(400, {"error": "verdict must include cleared boolean"})
        if not verdict["cleared"]:
            return _json_response(409, {"error": "crowd did not clear"})

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
            order = sandbox.create_order(purchase, verdict.get("summary", ""))
        except PayPalError as e:
            return _json_response(502, {"error": str(e)})
        response_body = {
            "order_id": order.get("id"),
            "status": order.get("status"),
            "approve_url": order.get("approve_url"),
            "verdict_summary": verdict.get("summary", ""),
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