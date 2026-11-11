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
    origin = purchase.get("origin")
    if origin is not None and origin not in ("self", "contacted", "known_person"):
        return False, "field origin must be one of: self, contacted, known_person"
    src = purchase.get("source")
    if src is not None and (not isinstance(src, str) or len(src) > 500):
        return False, "field source must be a string of at most 500 characters"
    verified = purchase.get("verified")
    if verified is not None and (not isinstance(verified, str) or len(verified) > 1000):
        return False, "field verified must be a string of at most 1000 characters"
    try:
        price = decimal.Decimal(purchase["price"])
        if price <= 0:
            return False, "price must be positive"
    except Exception:
        return False, "price must be a valid decimal"
    return True, ""

_PURCHASE_KEYS = ("item", "price", "currency", "terms", "need")
_SIGNED_KEYS = _PURCHASE_KEYS + ("origin", "source", "verified")  # the buyer's own statement of how they checked is part of what was cleared
_TOKEN_TTL = 300  # seconds a clearance stays valid


def _token_key() -> bytes:
    seed = os.getenv("SECONDLOOK_TOKEN_SEED") or os.getenv("PAYPAL_SECRET") or ""
    return hashlib.sha256(("secondlook-clearance:" + seed).encode("utf-8")).digest()


def _canonical(purchase: Dict[str, Any]) -> str:
    return json.dumps({k: str(purchase.get(k) or "") for k in _SIGNED_KEYS}, sort_keys=True, separators=(",", ":"))


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
    "info": {"title": "Second Look", "version": "1.2.0",
             "description": "A second look before money moves. Three AI reviewers from three makers check a purchase, and five when one stands alone. "
                            "The answer is allow, verify (locked until the buyer has checked the request themselves) or hold. "
                            "PayPal orders are created only with a clearance token issued for that exact purchase."},
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
            "terms": {"type": "string", "description": "The seller's terms, or the text of the payment request"},
            "origin": {"type": "string", "enum": ["self", "contacted", "known_person"], "description": "How the request reached the buyer. self: the buyer (or the agent's user) went to the seller on their own initiative. contacted: it came to them (email, text, call, letter, invoice, notice, advert, link). known_person: someone they know asked. A 'contacted' request is never cleared before the buyer has checked it themselves"},
            "source": {"type": "string", "description": "Optional. The buyer's own words on how the request reached them"},
            "verified": {"type": "string", "description": "Optional. After a 'verify' decision: how the buyer checked the request for themselves, in their own words"}}},
        "PurchaseRequest": {"type": "object", "required": ["purchase"], "properties": {"purchase": {"$ref": "#/components/schemas/Purchase"}}},
        "Decision": {"type": "object", "properties": {
            "decision": {"type": "string", "enum": ["allow", "verify", "hold"], "description": "allow: all reviewers cleared. hold: a scam signal. verify: a payment request the buyer has not checked for themselves; PayPal stays locked until they have"},
            "clear_count": {"type": "integer"}, "verify_steps": {"type": "array", "items": {"type": "string"}, "description": "On verify: checks the buyer can do without using anything in the message"},
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
.wrap{max-width:1280px;margin:0 auto}h1{font-size:26px;margin:0 0 6px}p{color:#555;margin:0 0 12px;line-height:1.5}
.sets{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 4px}.sets button{border:1px solid #c8ced6;background:#fff;color:#333;border-radius:8px;padding:9px 14px;font-weight:600;cursor:pointer;text-align:left}
.sets button small{display:block;font-weight:400;color:#777;font-size:12px}.sets button.on{border-color:#111;background:#111;color:#fff}.sets button.on small{color:#ccc}
.tiles{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}.tile{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px 18px;min-width:170px}
.tile b{display:block;font-size:26px}.tile span{color:#666;font-size:13px}
.bar{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}.bar button{border:1px solid #1a7fdb;background:#fff;color:#1a7fdb;border-radius:8px;padding:8px 14px;font-weight:600;cursor:pointer}
.bar button.on{background:#1a7fdb;color:#fff}#grid{height:640px;width:100%}a{color:#1a7fdb}#about{min-height:48px}
</style></head><body><div class="wrap">
<h1>Blind test results: every purchase, every answer</h1>
<p>Each test set was written by AI models from other makers, from a category list only. Each was run once, after the code was frozen, against pass marks written down beforehand. Sort, filter or search any column.
<a href="/">Back to the demo</a> &middot; <a href="https://github.com/DKJager/secondlook/tree/main/bench">Data and re-run scripts</a></p>
<div class="sets" id="sets"></div>
<p id="about"></p>
<div class="tiles" id="tiles"></div>
<div class="bar"><button data-f="all" class="on">All</button><button data-f="miss">Misses only</button><button data-f="scam">Scams</button><button data-f="normal">Normal purchases</button><button data-f="control">Not checked (control)</button><button data-f="check">Asked to check first</button>
<input id="q" placeholder="Search..." style="flex:1;min-width:180px;border:1px solid #cfd4da;border-radius:8px;padding:8px 12px">
<button id="wrap" data-on="0" title="Show every cell in full">Full text</button></div>
<div id="grid" class="ag-theme-quartz"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/ag-grid-community@32.3.3/dist/ag-grid-community.min.js"></script>
<script>
const ALL = __ROWS__;
const SETS = __SETS__;
let cur = SETS[0].id, mode = 'all';
const tile = (n, t) => '<div class="tile"><b>' + n + '</b><span>' + t + '</span></div>';
const rowsOf = () => ALL.filter(r => r.set === cur);
const colour = v => ({ color: v === 'hold' ? '#d93025' : (v === 'verify' ? '#a56a00' : '#1e8e3e'), fontWeight: 600 });
const word = v => v === 'verify' ? 'check first' : v;
const long = (field, headerName, minWidth) => ({ field, headerName, flex: 2, minWidth, tooltipField: field });
const cols = wrap => [
    { colId: 'result', headerName: 'Result', width: 105, valueGetter: p => p.data.correct ? 'Correct' : 'MISS',
      cellStyle: p => ({ color: p.value === 'Correct' ? '#1e8e3e' : '#d93025', fontWeight: 600 }) },
    { field: 'label', headerName: 'Truth', width: 110, valueFormatter: p => p.value === 'control' ? 'normal, not checked' : p.value },
    { field: 'first', headerName: 'First answer', width: 135, valueFormatter: p => word(p.value), cellStyle: p => colour(p.value) },
    { field: 'final', headerName: 'After the buyer checked', width: 150, valueFormatter: p => word(p.value), cellStyle: p => colour(p.value) },
    { field: 'origin', headerName: 'How it reached the buyer', width: 150, valueFormatter: p => ({ self: 'went to seller', contacted: 'came to buyer', known_person: 'someone they know' })[p.value] || 'not asked' },
    { field: 'item', width: 210, tooltipField: 'item' },
    { field: 'price', width: 100, valueFormatter: p => '$' + Number(p.value).toLocaleString() },
    long('need', 'Buyer said', 280),
    long('asked', 'Answer to "how did you check?"', 260),
    long('reasons', 'Reviewers said', 280),
    { field: 'writer', headerName: 'Written by', width: 140 }
  ].map(c => Object.assign(c, { wrapText: wrap, autoHeight: wrap }));
const api = agGrid.createGrid(document.getElementById('grid'), {
  rowData: rowsOf(),
  defaultColDef: { sortable: true, filter: true, resizable: true },
  tooltipShowDelay: 150,
  columnDefs: cols(false),
  isExternalFilterPresent: () => mode !== 'all',
  doesExternalFilterPass: n => mode === 'miss' ? !n.data.correct : (mode === 'check' ? n.data.first === 'verify' : n.data.label === mode)
});
function draw() {
  const rows = rowsOf(), scams = rows.filter(r => r.label === 'scam'), normal = rows.filter(r => r.label === 'normal'), ctl = rows.filter(r => r.label === 'control');
  const set = SETS.find(s => s.id === cur);
  document.getElementById('about').textContent = set.about;
  let t = '';
  if (set.lie) {
    t = tile(scams.filter(r => r.correct).length + ' of ' + scams.length, 'false claims stopped') +
        tile(scams.filter(r => !r.correct).length + ' of ' + scams.length, 'cleared on a false claim');
  } else {
    t = tile(scams.filter(r => r.correct).length + ' of ' + scams.length, 'scams stopped') +
        tile(normal.filter(r => r.final !== 'allow').length + ' of ' + normal.length, 'normal purchases wrongly blocked') +
        tile(normal.filter(r => r.first === 'hold' || r.final === 'hold').length + ' of ' + normal.length, 'normal purchases called a scam') +
        tile(normal.filter(r => r.first === 'allow').length + ' of ' + normal.length, 'normal purchases cleared at once') +
        tile(normal.filter(r => r.final === 'allow').length + ' of ' + normal.length, 'cleared after at most one check') +
        (ctl.length ? tile(ctl.filter(r => r.correct).length + ' of ' + ctl.length, 'unchecked requests kept locked') : '');
  }
  document.getElementById('tiles').innerHTML = t + tile(rows.filter(r => !r.correct).length, 'misses, all listed below');
  api.setGridOption('rowData', rows); api.onFilterChanged();
}
document.getElementById('sets').innerHTML = SETS.map(s => '<button data-s="' + s.id + '"' + (s.id === cur ? ' class="on"' : '') + '>' + s.name + '<small>' + s.sub + '</small></button>').join('');
document.querySelectorAll('.sets button').forEach(b => b.addEventListener('click', () => {
  cur = b.dataset.s; document.querySelectorAll('.sets button').forEach(x => x.classList.toggle('on', x === b)); draw();
}));
document.querySelectorAll('.bar button[data-f]').forEach(b => b.addEventListener('click', () => {
  mode = b.dataset.f; document.querySelectorAll('.bar button[data-f]').forEach(x => x.classList.toggle('on', x === b)); api.onFilterChanged();
}));
document.getElementById('wrap').addEventListener('click', e => {
  const on = e.target.dataset.on !== '1'; e.target.dataset.on = on ? '1' : '0'; e.target.classList.toggle('on', on);
  api.setGridOption('columnDefs', cols(on)); api.resetRowHeights();
});
document.getElementById('q').addEventListener('input', e => api.setGridOption('quickFilterText', e.target.value));
draw();
</script></body></html>"""


def _results_page() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_results.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    safe = lambda obj: json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    return _RESULTS_PAGE.replace("__SETS__", safe(data["sets"])).replace("__ROWS__", safe(data["rows"]))


def _first_answer(once: Callable, messages: List[Dict], hedge_after: float) -> str:
    """Call once(messages); if no answer after hedge_after seconds, call it a second time in parallel.
    Returns the first answer to arrive. Raises only if every attempt made has failed."""
    import queue
    import threading

    box: "queue.Queue" = queue.Queue()

    def run():
        try:
            box.put((True, once(messages)))
        except Exception as exc:  # noqa: BLE001
            box.put((False, exc))

    threading.Thread(target=run, daemon=True).start()
    started, failed, last = 1, 0, None
    try:
        ok, val = box.get(timeout=hedge_after)
    except queue.Empty:
        threading.Thread(target=run, daemon=True).start()
        started = 2
        ok, val = box.get()
    while not ok:
        failed += 1
        last = val
        if failed >= started:
            raise last
        ok, val = box.get()
    return val


def _build_ask_fns(models: List[str], max_tokens: int = None) -> List[Callable]:
    base_url = os.getenv("SECONDLOOK_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
    api_key = os.getenv("SECONDLOOK_API_KEY")
    if not api_key:
        raise CrowdError("SECONDLOOK_API_KEY is missing from environment")
    max_tokens = max_tokens or int(os.getenv("SECONDLOOK_MAX_TOKENS", "1500"))
    temperature = float(os.getenv("SECONDLOOK_TEMPERATURE", "0"))
    timeout = 40.0

    hedge_after = float(os.getenv("SECONDLOOK_HEDGE_SECONDS", "5"))

    def _make_fn(model_id: str) -> Callable:
        def once(messages: List[Dict]) -> str:
            response = chat(
                base_url=base_url,
                key=api_key,
                model=model_id,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
            )
            return response["choices"][0]["message"]["content"]

        def fn(messages: List[Dict]) -> str:
            # Speed only, never the decision: if this reviewer has not answered after hedge_after seconds, the same
            # question is sent to the same model a second time and whichever answer arrives first is used.
            try:
                if hedge_after <= 0:
                    return once(messages)
                return _first_answer(once, messages, hedge_after)
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
        .verdict-banner.verify { background: #fff3bf; color: #7a5200; }
        .verify-box { display: none; background: #fff9db; border: 1px solid #ffe066; border-radius: 10px; padding: 16px 18px; margin: 0 0 16px; }
        .verify-box.active { display: block; }
        .verify-box h3 { margin: 0 0 6px; font-size: 1rem; color: #7a5200; }
        .verify-box ul { margin: 6px 0 12px 18px; padding: 0; color: #333; }
        .verify-box li { margin: 3px 0; }
        .verify-box label { display: block; font-weight: 600; margin: 4px 0 6px; color: #333; }
        .verify-box textarea { display: block; width: 100%; box-sizing: border-box; min-height: 64px; margin-bottom: 12px; padding: 10px 12px; border: 1px solid #ced4da; border-radius: 8px; font: inherit; background: #fff; }
        .verify-box .hint { font-size: .85rem; color: #666; margin: 0 0 8px; }

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
        .badge-verify { background: #fff3bf; color: #7a5200; }
        .crowd-note { font-size: .88rem; color: #495057; margin: 10px 2px 0; }
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
            <p class="tagline">A second look before money moves. Built on <a href="https://3dogs.ai" style="color:#1a7fdb">3Dogs</a> wisdom of crowds: three AI reviewers from three makers check every purchase, and five when one stands alone. A scam is held. A request that came to you stays locked until you have checked it yourself. PayPal creates the order only with their signed clearance. <a href="/results" style="color:#1a7fdb">See every result from our blind test of 120 purchases</a>.</p>
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
                <label for="origin">How did this payment request reach you?</label>
                <select id="origin">
                    <option value="">Choose one</option>
                    <option value="self">I went to the seller myself (their site, app or shop)</option>
                    <option value="contacted">They contacted me (email, text, call, letter, invoice or a link)</option>
                    <option value="known_person">Someone I know asked me to pay</option>
                </select>
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
            <button type="button" id="loadQuiet" class="btn-outline">Load quiet invoice example</button>
            <button type="button" id="loadExample" class="btn-outline">Load normal example</button>
            <button id="askCrowd" class="btn-primary">Ask the crowd</button>
        </div>

        <div id="loadingState" class="loading-state">
            <div class="spinner"></div>
            Asking three models...
        </div>

        <div id="resultsSection" class="results-section">
            <div id="banner" class="verdict-banner" style="display:none;"></div>
            <div id="verifyBox" class="verify-box">
                <h3>Check it yourself first. This takes a minute.</h3>
                <p class="hint" id="verifyHint">Nothing here proves a scam. But this request came to you, and a real bill and a fake one can read exactly the same. Do one of these without using any link, number or reply address from the message:</p>
                <ul id="verifySteps"></ul>
                <p class="hint" style="color:#7a5200;font-weight:600;">If anyone told you what to write in this box, stop. That is how the scam works.</p>
                <label for="verified">How did you check?</label>
                <textarea id="verified" placeholder="For example: I logged in to my own account on their website and the invoice is there."></textarea>
                <button type="button" id="recheck" class="btn-primary">I checked. Look again</button>
                <p class="hint" id="verifyAgain" style="display:none;color:#c92a2a;font-weight:600;margin:12px 0 0;"></p>
            </div>
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
                <div class="reviewer-card" id="verdict3" style="display:none;">
                    <div class="reviewer-header">Reviewer 4 (larger crowd)</div>
                    <div class="model-id"></div>
                    <div class="badge"></div>
                    <div class="reason"></div>
                    <div class="quote"></div>
                </div>
                <div class="reviewer-card" id="verdict4" style="display:none;">
                    <div class="reviewer-header">Reviewer 5 (larger crowd)</div>
                    <div class="model-id"></div>
                    <div class="badge"></div>
                    <div class="reason"></div>
                    <div class="quote"></div>
                </div>
            </div>
            <p id="crowdNote" class="crowd-note" style="display:none;"></p>
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
        <p>Second Look &mdash; open-source, Apache-2.0. Built on 3Dogs wisdom of crowds: three reviewers for speed, five when one stands alone. PayPal Orders v2 sandbox.</p>
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
                document.getElementById('origin').value = 'contacted';
                document.getElementById('need').value = 'My grandson is in jail and needs bail tonight. The officer on the phone said to read him the card numbers.';
                document.getElementById('terms').value = 'Gift cards are non-refundable and cannot be exchanged for cash. Standard shipping 3-5 days.';
            });

            document.getElementById('loadQuiet').addEventListener('click', function() {
                document.getElementById('item').value = 'Website hosting, yearly renewal';
                document.getElementById('price').value = '240.00';
                document.getElementById('currency').value = 'USD';
                document.getElementById('origin').value = 'contacted';
                document.getElementById('need').value = 'I got an email invoice for our yearly website hosting. It says their bank details changed, so I should pay the new account.';
                document.getElementById('terms').value = 'Annual hosting renewal. Our banking details have been updated; please remit to the account on this invoice within 10 days to avoid interruption.';
                document.getElementById('verified').value = '';
            });

            loadExampleBtn.addEventListener('click', function() {
                document.getElementById('item').value = 'Premium Wireless Headphones';
                document.getElementById('price').value = '129.99';
                document.getElementById('currency').value = 'USD';
                document.getElementById('origin').value = 'self';
                document.getElementById('need').value = 'Need high-quality headphones for daily commute and work calls';
                document.getElementById('terms').value = 'Limited Warranty: 1 year. Return Policy: 30-day money-back guarantee. Note: no returns on opened items. Shipping: free standard shipping.';
            });

            function readPurchase(withVerified) {
                const p = {
                    item: document.getElementById('item').value,
                    price: document.getElementById('price').value,
                    currency: document.getElementById('currency').value,
                    terms: document.getElementById('terms').value,
                    need: document.getElementById('need').value
                };
                const o = document.getElementById('origin').value;
                if (o) { p.origin = o; }
                const v = document.getElementById('verified').value.trim();
                if (withVerified && v) { p.verified = v; }
                return p;
            }
            window.lastPurchase = null;
            document.getElementById('recheck').addEventListener('click', function() {
                if (!document.getElementById('verified').value.trim()) { document.getElementById('verified').focus(); return; }
                runCheck(true);
            });
            askCrowdBtn.addEventListener('click', function() { document.getElementById('verified').value = ''; runCheck(false); });
            async function runCheck(withVerified) {
                const purchase = readPurchase(withVerified);
                window.lastPurchase = purchase;
                document.getElementById('verifyBox').classList.remove('active');

                errorNote.classList.remove('active');
                errorNote.textContent = '';
                resultsSection.classList.add('active');
                loadingState.classList.add('active');

                // hide previous results and payment info while loading
                document.getElementById('banner').style.display = 'none';
                for (let i = 0; i < 5; i++) {
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
                        } else if (verdict.verdict === 'verify') {
                            badge.textContent = 'VERIFY FIRST';
                            badge.className = 'badge badge-verify';
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

                    const note = document.getElementById('crowdNote');
                    if (result.escalated) {
                        note.textContent = 'One of the first three reviewers stood alone, so the question went to two more makers. ' +
                            (result.cleared ? 'Neither shared the doubt.' : (result.objection_not_confirmed ? 'They did not confirm a scam, so this is a check, not a block.' : (result.decision === 'hold' ? 'The objection was confirmed.' : 'The doubt was shared.')));
                        note.style.display = 'block';
                    } else { note.style.display = 'none'; }

                    // Display banner
                    const banner = document.getElementById('banner');
                    if (result.cleared) {
                        banner.textContent = 'CLEARED by ' + result.clear_count + '/' + verdicts.length + ' reviewers' + (purchase.verified ? ' after your check' : '');
                        banner.className = 'verdict-banner cleared';
                        confirmPayBtn.disabled = false;
                    } else if (result.decision === 'verify') {
                        banner.textContent = purchase.verified
                            ? 'STILL LOCKED: that check does not count yet'
                            : 'VERIFY FIRST: PayPal stays locked until you have checked this yourself';
                        banner.className = 'verdict-banner verify';
                        confirmPayBtn.disabled = true;
                        const again = document.getElementById('verifyAgain');
                        const doubt = verdicts.find(function(v) { return v.verdict !== 'clear' && v.reason; });
                        if (purchase.verified && doubt) { again.textContent = 'Why it does not count: ' + doubt.reason; again.style.display = 'block'; }
                        else { again.style.display = 'none'; }
                        const ul = document.getElementById('verifySteps'); ul.innerHTML = '';
                        (result.verify_steps || []).forEach(function(t) { const li = document.createElement('li'); li.textContent = t; ul.appendChild(li); });
                        document.getElementById('verifyHint').textContent = result.objection_not_confirmed
                            ? 'One reviewer objected and the others did not see a scam. So this is a check, not a block. Do this first, then say how you checked:'
                            : (purchase.origin === 'contacted'
                                ? 'Nothing here proves a scam. But this request came to you, and a real bill and a fake one can read exactly the same. Do one of these without using any link, number or reply address from the message:'
                                : 'Nothing here proves a scam, but something should be checked before money moves. Do one of these first:');
                        document.getElementById('verifyBox').classList.add('active');
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
            }

            confirmPayBtn.addEventListener('click', async function() {
                const purchase = window.lastPurchase || readPurchase(true);

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
            "openai/gpt-oss-120b,Qwen/Qwen3-30B-A3B-Instruct-2507,google/gemma-3-27b-it",
        )
        models = [m.strip() for m in models_env.split(",") if m.strip()]
        if not models:
            return _json_response(503, {"error": "no models configured"})

        ask_fns = _build_ask_fns(models)
        extra_models = [m.strip() for m in os.getenv("SECONDLOOK_EXTRA_MODELS", "moonshotai/Kimi-K3,meta-models/muse-spark-open").split(",") if m.strip()]
        # the larger crowd uses reasoning models, which need room to think before they answer
        extra_fns = _build_ask_fns(extra_models, max_tokens=int(os.getenv("SECONDLOOK_EXTRA_MAX_TOKENS", "2500"))) if extra_models else []

        try:
            result = judge_purchase(purchase, ask_fns, min_clear=int(os.getenv("SECONDLOOK_MIN_CLEAR", "3")), extra_fns=extra_fns)
        except CrowdError as e:
            return _json_response(500, {"error": str(e)})

        # Ensure models are present in the response for the UI to display
        if "models" not in result:
            result["models"] = models + (extra_models if result.get("escalated") else [])
        models = result["models"]
        result["token"] = _sign_clearance(purchase) if result.get("cleared") else None

        if path == "/agent/check":
            return _json_response(200, {
                "decision": result.get("decision") or ("allow" if result.get("cleared") else "hold"),
                "clear_count": result.get("clear_count"),
                "verify_steps": result.get("verify_steps", []),
                "escalated": bool(result.get("escalated")),
                "reviews": [{"model": models[v["model_index"]] if v["model_index"] < len(models) else "",
                             "verdict": v["verdict"], "reason": v["reason"], "quote": v["quote"],
                             "quote_from": v.get("quote_from"), "step": v.get("step")} for v in result.get("verdicts", [])],
                "clearance_token": result["token"],
                "next": ("POST /order with {purchase, clearance_token}" if result.get("cleared")
                         else "Do not pay yet. Ask the buyer to do one of verify_steps themselves, then call again with purchase.verified set to how they checked. An agent must not answer this for the buyer."
                         if result.get("decision") == "verify" else "Do not pay. Show the reasons to the buyer."),
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
            order = sandbox.create_order(purchase, "Second Look: cleared by the reviewer crowd", request_id="secondlook-" + nonce)
        except PayPalError as e:
            return _json_response(502, {"error": str(e)})
        response_body = {
            "order_id": order.get("id"),
            "status": order.get("status"),
            "approve_url": order.get("approve_url"),
            "verdict_summary": "Second Look: cleared by the reviewer crowd",
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