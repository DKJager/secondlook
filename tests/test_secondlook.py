import sys, os, json, base64, uuid, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.secondlook import llm, crowd, paypal, handler
from src.secondlook.llm import SecondLookError as LLMError
from src.secondlook.crowd import SecondLookError as CrowdError
from src.secondlook.paypal import SecondLookError as PayPalError

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
class FakeResponse:
    def __init__(self, data: bytes, code: int = 200, headers: dict = None):
        self._data = data
        self._code = code
        self.headers = headers or {}

    def read(self):
        return self._data

    def getcode(self):
        return self._code

class FakeOpener:
    """A minimal urllib opener that records every request."""
    def __init__(self, responses: list):
        """
        responses: an iterator of FakeResponse objects to return for each open() call.
        """
        self._responses = responses
        self.requests = []  # list of urllib.request.Request objects

    def open(self, request, timeout=None):
        self.requests.append(request)
        try:
            return self._responses.pop(0)
        except IndexError:
            # Default empty 200 response
            return FakeResponse(b'{}')

# ----------------------------------------------------------------------
# 1. LLM Chat test
# ----------------------------------------------------------------------
def test_llm_posts_bearer_and_parses():
    """Ensure chat() posts Authorization header and parses JSON response."""
    fake_resp = FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
    opener = FakeOpener([fake_resp])

    base_url = "https://api.fake.com"
    api_key = "test-key-123"
    model = "model-x"
    messages = [{"role": "user", "content": "hello"}]
    result = llm.chat(
        base_url=base_url,
        key=api_key,
        model=model,
        messages=messages,
        max_tokens=5,
        temperature=0.0,
        timeout=1.0,
        opener=opener,
    )

    # Exactly one request was made
    assert len(opener.requests) == 1, "Expected a single HTTP request"
    req = opener.requests[0]

    # URL should be base_url + /v1/chat/completions
    expected_url = urllib.parse.urljoin(base_url, "/v1/chat/completions")
    assert req.full_url == expected_url, f"Unexpected URL {req.full_url!r}"

    # Header Authorization Bearer token (case‑insensitive)
    auth_headers = [h for h in req.header_items() if h[0].lower() == "authorization"]
    assert auth_headers, "Missing Authorization header"
    auth_val = auth_headers[0][1]
    assert auth_val == f"Bearer {api_key}", f"Bad Authorization header {auth_val!r}"

    # Request payload should contain model and messages
    payload = json.loads(req.data.decode())
    assert payload["model"] == model, "Model not in request payload"
    assert payload["messages"] == messages, "Messages not in request payload"
    assert payload["max_tokens"] == 5, "max_tokens not in request payload"

    # Result should be parsed JSON dict
    assert isinstance(result, dict), "Result is not a dict"
    assert "choices" in result, "Parsed result missing expected keys"

# ----------------------------------------------------------------------
# 2. Crowd clears when all clear
# ----------------------------------------------------------------------
def test_crowd_clears_when_all_clear():
    purchase = {
        "item": "Gadget",
        "price": "99.99",
        "currency": "USD",
        "terms": "All good. No returns policy.",
        "need": "I need it.",
    }

    # Three ask_fns returning clear verdicts
    def make_clear(i):
        return lambda msgs: json.dumps({"verdict": "clear", "reason": f"Reason {i}", "quote": None})

    ask_fns = [make_clear(i) for i in range(3)]

    result = crowd.judge_purchase(purchase, ask_fns)

    # Should have three verdicts, all clear
    assert len(result["verdicts"]) == 3, "Expected three verdict entries"
    for v in result["verdicts"]:
        assert v["verdict"] == "clear", "Expected clear verdict"

    assert result["clear_count"] == 3, "Clear count should be 3"
    assert result["cleared"] is True, "Should be cleared when all are clear"
    assert "3/3 clear" in result["summary"], "Summary missing clear count"

# ----------------------------------------------------------------------
# 3. Crowd holds on one dissent and keeps reason
# ----------------------------------------------------------------------
def test_crowd_holds_on_one_dissent_and_keeps_reason():
    purchase = {
        "item": "Gadget",
        "price": "199.00",
        "currency": "USD",
        "terms": "No returns on opened items. Full warranty.",
        "need": "I need it.",
    }

    def clear_fn(msgs):
        return json.dumps({"verdict": "clear", "reason": "All good", "quote": None})

    def hold_fn(msgs):
        return json.dumps({
            "verdict": "hold",
            "reason": "No returns on opened items",
            "quote": "No returns on opened items"
        })

    ask_fns = [clear_fn, clear_fn, hold_fn]

    result = crowd.judge_purchase(purchase, ask_fns)

    assert result["cleared"] is False, "Overall should not be cleared"
    assert result["clear_count"] == 2, "Clear count should be 2"
    # The hold reason must appear in the summary
    assert "No returns on opened items" in result["summary"], "Hold reason missing in summary"

# ----------------------------------------------------------------------
# 4. Crowd drops quote not in terms
# ----------------------------------------------------------------------
def test_crowd_drops_quote_not_in_terms():
    purchase = {
        "item": "Widget",
        "price": "49.99",
        "currency": "USD",
        "terms": "Standard warranty. No returns on opened items.",
        "need": "Needed quickly.",
    }

    def hold_wrong_quote(msgs):
        return json.dumps({
            "verdict": "hold",
            "reason": "Quote mismatch",
            "quote": "Non‑existent phrase"
        })

    ask_fns = [hold_wrong_quote]  # single reviewer for simplicity

    result = crowd.judge_purchase(purchase, ask_fns)

    v = result["verdicts"][0]
    assert v["quote"] is None, "Quote not found should be set to null"
    # Reason should be amended with the indicator
    assert "(quote not found in terms)" in v["reason"], "Reason not annotated for missing quote"
    assert result["cleared"] is False, "Should be held"

# ----------------------------------------------------------------------
# 5. Crowd unparseable counts as hold
# ----------------------------------------------------------------------
def test_crowd_unparseable_counts_as_hold():
    purchase = {
        "item": "Thing",
        "price": "10.00",
        "currency": "USD",
        "terms": "All terms are fine.",
        "need": "Just want it.",
    }

    def good_fn(msgs):
        return json.dumps({"verdict": "clear", "reason": "All fine", "quote": None})

    def bad_fn(msgs):
        return "this is not json"

    ask_fns = [good_fn, bad_fn, good_fn]

    result = crowd.judge_purchase(purchase, ask_fns)

    # Expect one hold due to parse failure
    holds = [v for v in result["verdicts"] if v["verdict"] == "hold"]
    assert len(holds) == 1, "Expected exactly one hold from unparseable answer"
    assert holds[0]["reason"] == "unparseable answer", "Incorrect reason for unparseable answer"
    assert result["clear_count"] == 2, "Clear count should be 2"
    assert result["cleared"] is False, "Overall should be not cleared"

# ----------------------------------------------------------------------
# 6. PayPal create order body has verdict and truncates
# ----------------------------------------------------------------------
def test_paypal_create_order_body_has_verdict_and_truncates():
    # Prepare fake token response and order response
    token_resp = FakeResponse(json.dumps({"access_token": "fake-token"}).encode())
    order_resp_body = {
        "id": "ORDER123",
        "status": "CREATED",
        "links": [
            {"rel": "approve", "href": "https://paypal.com/approve"},
            {"rel": "self", "href": "https://paypal.com/self"}
        ]
    }
    order_resp = FakeResponse(json.dumps(order_resp_body).encode())
    opener = FakeOpener([token_resp, order_resp])

    client_id = "client-id"
    secret = "secret"
    sandbox = paypal.PayPalSandbox(client_id, secret, opener=opener)

    purchase = {"item": "Gadget", "price": "129.00", "currency": "USD"}
    long_summary = "x" * 300  # 300 chars, will be truncated to 127

    result = sandbox.create_order(purchase, long_summary)

    # Two requests: token then order
    assert len(opener.requests) == 2, "Expected token and order requests"

    # Verify token request details
    token_req = opener.requests[0]
    assert token_req.full_url.endswith("/v1/oauth2/token"), "Token request URL incorrect"
    auth_header = [h for h in token_req.header_items() if h[0].lower() == "authorization"]
    assert auth_header, "Missing Authorization header on token request"

    # Verify order request details
    order_req = opener.requests[1]
    assert order_req.full_url.endswith("/v2/checkout/orders"), "Order request URL incorrect"

    # Header PayPal-Request-Id present (case‑insensitive)
    rid_headers = [h for h in order_req.header_items() if h[0].lower() == "paypal-request-id"]
    assert rid_headers, "Missing PayPal-Request-Id header"

    # Payload checks
    payload = json.loads(order_req.data.decode())
    pu = payload["purchase_units"][0]

    # amount fields
    assert pu["amount"]["currency_code"] == "USD", "Currency code mismatch"
    assert pu["amount"]["value"] == "129.00", "Price mismatch"

    # description truncation
    expected_desc = long_summary[:127]
    assert pu["description"] == expected_desc, "Description not truncated correctly"

    # custom_id should start with secondlook:
    assert pu["custom_id"].startswith("secondlook:"), "custom_id format incorrect"

    # Approve URL extraction
    assert result["id"] == "ORDER123", "Returned order id incorrect"
    assert result["status"] == "CREATED", "Returned status incorrect"
    assert result["approve_url"] == "https://paypal.com/approve", "Approve URL not extracted"

# ----------------------------------------------------------------------
# 7. PayPal capture posts to capture URL
# ----------------------------------------------------------------------
def test_paypal_capture_posts_to_capture_url():
    token_resp = FakeResponse(json.dumps({"access_token": "tok"}).encode())
    capture_resp_body = {"id": "CAP123", "status": "COMPLETED"}
    capture_resp = FakeResponse(json.dumps(capture_resp_body).encode())
    opener = FakeOpener([token_resp, capture_resp])

    sandbox = paypal.PayPalSandbox("id", "secret", opener=opener)

    result = sandbox.capture_order("CAP123")

    # Two requests: token then capture
    assert len(opener.requests) == 2, "Expected token and capture requests"

    cap_req = opener.requests[1]
    assert "/v2/checkout/orders/CAP123/capture" in cap_req.full_url, "Capture URL incorrect"

    # Header PayPal-Request-Id present
    rid = [h for h in cap_req.header_items() if h[0].lower() == "paypal-request-id"]
    assert rid, "Missing PayPal-Request-Id on capture"

    # Result verification
    assert result["id"] == "CAP123", "Capture result id mismatch"
    assert result["status"] == "COMPLETED", "Capture result status mismatch"

# ----------------------------------------------------------------------
# 8. Handler order refuses when not cleared
# ----------------------------------------------------------------------
def test_handler_order_refuses_when_not_cleared():
    # Ensure required env vars are present
    os.environ["PAYPAL_CLIENT_ID"] = "cid"
    os.environ["PAYPAL_SECRET"] = "csecret"
    os.environ["SECONDLOOK_API_KEY"] = "apikey"
    os.environ["SECONDLOOK_RETURN_URL"] = "https://example.com/return"

    # Use a fake opener that records requests; we will inject it via monkeypatching
    fake_opener = FakeOpener([])  # no responses needed because it shouldn't be called

    # Monkeypatch PayPalSandbox to use our fake opener
    original_init = paypal.PayPalSandbox.__init__

    def patched_init(self, client_id, secret, base_url="https://api-m.sandbox.paypal.com", opener=None):
        original_init(self, client_id, secret, base_url, opener=fake_opener)

    paypal.PayPalSandbox.__init__ = patched_init

    try:
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/order"}},
            "body": json.dumps({
                "purchase": {
                    "item": "Thing",
                    "price": "10.00",
                    "currency": "USD",
                    "terms": "All good",
                    "need": "Just need it"
                },
                "verdict": {
                    "cleared": False,
                    "clear_count": 0,
                    "summary": "0/3 clear"
                }
            })
        }

        resp = handler.lambda_handler(event, None)

        # Expect 409 response and no PayPal requests
        assert resp["statusCode"] == 409, "Expected 409 when verdict not cleared"
        assert not fake_opener.requests, "PayPal requests should not be made when not cleared"
    finally:
        # Restore original __init__
        paypal.PayPalSandbox.__init__ = original_init

# ----------------------------------------------------------------------
# 9. Handler GET serves HTML and validation limits
# ----------------------------------------------------------------------
def test_handler_get_serves_html_and_limits():
    # Ensure no PayPal env vars so missing-key path used for validation tests
    os.environ.pop("PAYPAL_CLIENT_ID", None)
    os.environ.pop("PAYPAL_SECRET", None)
    os.environ.pop("SECONDLOOK_API_KEY", None)

    # GET request
    get_event = {
        "requestContext": {"http": {"method": "GET", "path": "/"}},
    }
    get_resp = handler.lambda_handler(get_event, None)
    assert get_resp["statusCode"] == 200, "GET should return 200"
    headers = {k.lower(): v for k, v in get_resp.get("headers", {}).items()}
    assert headers.get("content-type", "").startswith("text/html"), "Content-Type should be text/html"
    body = get_resp.get("body", "")
    assert body.startswith("<!DOCTYPE html>"), "HTML body must start with <!DOCTYPE html>"

    # POST /judge with bad price
    bad_price_event = {
        "requestContext": {"http": {"method": "POST", "path": "/judge"}},
        "body": json.dumps({
            "item": "X",
            "price": "abc",  # invalid
            "currency": "USD",
            "terms": "ok",
            "need": "need"
        })
    }
    bad_price_resp = handler.lambda_handler(bad_price_event, None)
    assert bad_price_resp["statusCode"] == 400, "Invalid price should yield 400"

    # POST /judge with oversized terms (5000 chars > 4000)
    oversized_terms = "a" * 5000
    oversized_event = {
        "requestContext": {"http": {"method": "POST", "path": "/judge"}},
        "body": json.dumps({
            "item": "X",
            "price": "10.00",
            "currency": "USD",
            "terms": oversized_terms,
            "need": "need"
        })
    }
    oversized_resp = handler.lambda_handler(oversized_event, None)
    assert oversized_resp["statusCode"] == 400, "Oversized terms should yield 400"

# ----------------------------------------------------------------------
# 10. README names API models and LICENSE is Apache
# ----------------------------------------------------------------------
def test_readme_names_api_models_and_license_is_apache():
    # Verify README mentions license identifier
    readme_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md")
    with open(readme_path, "r", encoding="utf-8") as f:
        readme = f.read()
    assert "Apache-2.0" in readme, "README must contain Apache-2.0 license identifier"

    # Verify README lists default model IDs
    default_models = [
        "nvidia/nemotron-3-super-120b-a12b",
        "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "google/gemma-3-27b-it"
    ]
    for model_id in default_models:
        assert model_id in readme, f"README missing default model ID {model_id}"

    # Verify LICENSE file contains required strings
    license_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "LICENSE")
    with open(license_path, "r", encoding="utf-8") as f:
        license_text = f.read()
    assert "Apache License" in license_text, "LICENSE must contain 'Apache License'"
    assert "Version 2.0" in license_text, "LICENSE must contain 'Version 2.0'"

# ----------------------------------------------------------------------
# Execute when run directly
# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Collect test functions defined above
    test_funcs = [
        test_llm_posts_bearer_and_parses,
        test_crowd_clears_when_all_clear,
        test_crowd_holds_on_one_dissent_and_keeps_reason,
        test_crowd_drops_quote_not_in_terms,
        test_crowd_unparseable_counts_as_hold,
        test_paypal_create_order_body_has_verdict_and_truncates,
        test_paypal_capture_posts_to_capture_url,
        test_handler_order_refuses_when_not_cleared,
        test_handler_get_serves_html_and_limits,
        test_readme_names_api_models_and_license_is_apache,
    ]
    for func in test_funcs:
        try:
            func()
        except AssertionError as e:
            print(f"{func.__name__} FAILED: {e}", file=sys.stderr)
            sys.exit(1)
    print("ALL TESTS PASSED")


# ----------------------------------------------------------------------
# 9 Nov 2026: clearance token, quotes from the buyer's words, refusal retry, agent endpoint
# ----------------------------------------------------------------------
import time as _time

_P = {"item": "Kids bicycle", "price": "149.99", "currency": "USD", "terms": "Free shipping.", "need": "Birthday present"}


def _event(method, path, body=None):
    return {"requestContext": {"http": {"method": method, "path": path}}, "rawPath": path,
            "body": json.dumps(body) if body is not None else ""}


def test_clearance_token_binds_to_exact_purchase_and_expires(monkeypatch):
    monkeypatch.setenv("SECONDLOOK_TOKEN_SEED", "unit-test-seed")
    token = handler._sign_clearance(_P)
    nonce = handler._verify_clearance(_P, token)
    assert nonce and token.split(".")[1] == nonce
    assert handler._verify_clearance({**_P, "price": "900.00"}, token) is None      # changed price
    assert handler._verify_clearance({**_P, "need": "bail tonight"}, token) is None  # changed reason
    assert handler._verify_clearance(_P, handler._sign_clearance(_P, now=_time.time() - 400)) is None  # expired
    assert handler._sign_clearance(_P) != handler._sign_clearance(_P)                # a fresh nonce every time


def test_order_refuses_forged_or_missing_clearance(monkeypatch):
    monkeypatch.setenv("SECONDLOOK_TOKEN_SEED", "unit-test-seed")
    good = handler._sign_clearance(_P)
    forged = [
        {"verdict": {"cleared": True, "summary": "3/3 clear"}},   # the old trust-the-browser request
        {"clearance_token": "9999999999.abc.AAAA"},               # made-up token
        {"clearance_token": good[:-2] + "zz"},                    # tampered signature
        {},                                                       # nothing at all
    ]
    for extra in forged:
        resp = handler.lambda_handler(_event("POST", "/order", {"purchase": _P, **extra}), None)
        assert resp["statusCode"] == 409, extra
    # a real token for a different purchase is refused too
    resp = handler.lambda_handler(_event("POST", "/order", {"purchase": {**_P, "price": "1.00"}, "clearance_token": good}), None)
    assert resp["statusCode"] == 409


def test_quote_may_come_from_buyer_words_and_is_labelled():
    purchase = {"item": "Gift cards x6", "price": "900", "currency": "USD",
                "terms": "Gift cards are non-refundable.", "need": "My grandson is in jail and needs bail tonight"}
    answers = [
        '{"verdict":"hold","reason":"family bail scam","quote":"My grandson is in jail and needs bail tonight"}',
        '{"verdict":"hold","reason":"gift cards","quote":"Gift cards are non-refundable."}',
        '{"verdict":"hold","reason":"scam","quote":"words that appear nowhere"}',
    ]
    fns = [(lambda a: (lambda messages: a))(a) for a in answers]
    out = crowd.judge_purchase(purchase, fns, min_clear=3)
    v = out["verdicts"]
    assert out["cleared"] is False
    assert v[0]["quote_from"] == "need" and v[0]["quote"].startswith("My grandson")
    assert v[1]["quote_from"] == "terms"
    assert v[2]["quote"] is None and v[2]["quote_from"] is None   # an invented quote is dropped


def test_refusal_is_retried_once_and_not_counted_as_a_reasoned_hold():
    calls = []

    def refuses_then_reviews(messages):
        calls.append(len(messages))
        if len(calls) == 1:
            return '{"verdict":"hold","reason":"User requests a purchase transaction, which is disallowed","quote":null}'
        return '{"verdict":"clear","reason":"ordinary purchase","quote":null}'

    purchase = {"item": "Dog food", "price": "54.99", "currency": "USD", "terms": "Free shipping.", "need": "Monthly order"}
    out = crowd.judge_purchase(purchase, [refuses_then_reviews], min_clear=1)
    assert len(calls) == 2 and calls[1] == calls[0] + 2   # asked again, with the refusal and a correction appended
    assert out["cleared"] is True


def test_openapi_and_page_expose_agent_check_and_scam_example():
    spec = json.loads(handler.lambda_handler(_event("GET", "/openapi.json"), None)["body"])
    assert "/agent/check" in spec["paths"] and "/order" in spec["paths"]
    page = handler.lambda_handler(_event("GET", "/"), None)["body"]
    assert "Load scam example" in page and "Load normal example" in page


def test_agent_guard_never_calls_paypal_on_hold_and_binds_request_id_on_allow(monkeypatch):
    from agent.guard import SecondLookGuard
    made = []

    class FakeAPI:
        def __init__(self, request_id):
            self.request_id = request_id

        def run(self, method, params):
            made.append((self.request_id, method, params))
            return json.dumps({"id": "ORDER1", "status": "CREATED", "links": [{"rel": "approve", "href": "https://paypal.example/approve"}]})

    guard = SecondLookGuard("id", "secret", paypal_api_factory=FakeAPI)
    monkeypatch.setattr(guard, "check", lambda p: {"decision": "hold", "clearance_token": None,
                        "reviews": [{"model": "m1", "verdict": "hold", "reason": "bail scam", "quote": "needs bail tonight"}]})
    held = guard.checked_create_order("Gift cards x6", 900, "My grandson needs bail tonight", "Non-refundable.")
    assert held["status"] == "HELD" and held["paypal_called"] is False and made == []
    assert held["reasons"][0]["reason"] == "bail scam"

    monkeypatch.setattr(guard, "check", lambda p: {"decision": "allow", "clearance_token": "1999999999.NONCE123.sig", "reviews": []})
    ok = guard.checked_create_order("Kids bicycle", 149.99, "Birthday present", "30-day returns.")
    assert ok["status"] == "ORDER_CREATED" and ok["order_id"] == "ORDER1"
    assert made[0][0] == "secondlook-NONCE123" and made[0][1] == "create_order"
    assert made[0][2]["items"][0]["item_total"] == 149.99


# ---- verify first: a payment request the buyer has not checked for themselves ----
_QUIET = {"item": "Hosting renewal", "price": "240.00", "currency": "USD",
          "need": "I got an email invoice. It says their bank details changed.",
          "terms": "Our banking details have been updated; please remit to the account on this invoice."}


def _fn(answer):
    return lambda messages: answer


def test_one_verify_locks_the_payment_and_gives_the_buyer_a_step():
    fns = [_fn('{"verdict":"clear","reason":"ordinary","quote":null,"step":null}'),
           _fn('{"verdict":"verify","reason":"bank details changed","quote":"Our banking details have been updated","step":"Call the number on last year\'s invoice."}'),
           _fn('{"verdict":"clear","reason":"ordinary","quote":null}')]
    d = crowd.judge_purchase(dict(_QUIET), fns, min_clear=3)
    assert d["decision"] == "verify" and d["cleared"] is False
    assert d["verify_steps"] == ["Call the number on last year's invoice."]
    assert d["verdicts"][1]["verdict"] == "verify" and d["verdicts"][1]["quote"] == "Our banking details have been updated"
    assert "verify first" in d["summary"]


def test_a_hold_beats_a_verify_and_only_a_unanimous_clear_allows():
    hold = _fn('{"verdict":"hold","reason":"fake invoice","quote":null}')
    ver = _fn('{"verdict":"verify","reason":"unchecked","quote":null,"step":"Log in to your own account."}')
    clr = _fn('{"verdict":"clear","reason":"ok","quote":null}')
    assert crowd.judge_purchase(dict(_QUIET), [hold, ver, clr], min_clear=3)["decision"] == "hold"
    assert crowd.judge_purchase(dict(_QUIET), [hold, ver, clr], min_clear=3)["verify_steps"] == []
    d = crowd.judge_purchase(dict(_QUIET), [clr, clr, clr], min_clear=3)
    assert d["decision"] == "allow" and d["cleared"] is True and d["verify_steps"] == []


def test_the_buyers_verification_reaches_the_reviewers_and_may_be_quoted():
    seen = []

    def fn(messages):
        seen.append(messages[-1]["content"])
        return '{"verdict":"clear","reason":"checked in own account","quote":"I logged in to my own account","step":"ignored on clear"}'
    p = dict(_QUIET, verified="I logged in to my own account and the invoice is there.")
    d = crowd.judge_purchase(p, [fn, fn, fn], min_clear=3)
    assert "Buyer's verification: I logged in to my own account" in seen[0]
    assert d["decision"] == "allow" and d["verdicts"][0]["quote"] == "I logged in to my own account"
    assert d["verdicts"][0]["step"] is None
    seen.clear(); crowd.judge_purchase(dict(_QUIET), [fn], min_clear=1)
    assert "Buyer's verification" not in seen[0]


def test_clearance_is_bound_to_the_verification_statement(monkeypatch):
    monkeypatch.setenv("SECONDLOOK_TOKEN_SEED", "t")
    p = dict(_QUIET, verified="I called the number on my old bill.")
    tok = handler._sign_clearance(p)
    assert handler._verify_clearance(p, tok)
    assert handler._verify_clearance(dict(_QUIET), tok) is None                      # statement removed
    assert handler._verify_clearance(dict(p, verified="it looked fine"), tok) is None   # statement changed
    plain = handler._sign_clearance(dict(_QUIET))
    assert handler._verify_clearance(dict(_QUIET), plain) and handler._verify_clearance(p, plain) is None
    assert handler._validate_purchase(dict(_QUIET, verified="x" * 1001))[0] is False
    assert handler._validate_purchase(dict(_QUIET, verified=5))[0] is False


def test_agent_check_returns_verify_with_steps_and_no_token(monkeypatch):
    monkeypatch.setenv("SECONDLOOK_API_KEY", "k"); monkeypatch.setenv("SECONDLOOK_TOKEN_SEED", "t")
    monkeypatch.setenv("SECONDLOOK_MODELS", "a,b,c")
    ans = '{"verdict":"verify","reason":"bank details changed","quote":null,"step":"Call the number on an old invoice."}'
    monkeypatch.setattr(handler, "_build_ask_fns", lambda models, **kw: [_fn(ans)] * len(models))
    ev = {"requestContext": {"http": {"method": "POST", "path": "/agent/check"}}, "rawPath": "/agent/check", "body": json.dumps({"purchase": _QUIET})}
    r = handler.lambda_handler(ev, None); b = json.loads(r["body"])
    assert r["statusCode"] == 200 and b["decision"] == "verify" and b["clearance_token"] is None
    assert b["verify_steps"] == ["Call the number on an old invoice."] and "must not answer this for the buyer" in b["next"]
    assert b["reviews"][0]["step"] == "Call the number on an old invoice."
    spec = handler._OPENAPI["components"]["schemas"]
    assert spec["Decision"]["properties"]["decision"]["enum"] == ["allow", "verify", "hold"] and "verified" in spec["Purchase"]["properties"]


def test_agent_guard_does_not_call_paypal_on_verify_and_passes_the_buyers_check(monkeypatch):
    from agent.guard import SecondLookGuard
    made, sent = [], []

    class FakeAPI:
        def __init__(self, request_id): self.request_id = request_id
        def run(self, method, params):
            made.append(self.request_id); return json.dumps({"id": "O2", "status": "CREATED", "links": []})

    guard = SecondLookGuard("id", "secret", paypal_api_factory=FakeAPI)

    def check(p):
        sent.append(p)
        if "verified" not in p:
            return {"decision": "verify", "clearance_token": None, "verify_steps": ["Log in to your own account."],
                    "reviews": [{"model": "m", "verdict": "verify", "reason": "bank details changed", "quote": None}]}
        return {"decision": "allow", "clearance_token": "1999999999.N2.sig", "reviews": []}
    monkeypatch.setattr(guard, "check", check)
    first = guard.checked_create_order("Hosting renewal", 240, _QUIET["need"], _QUIET["terms"])
    assert first["status"] == "VERIFY_FIRST" and first["paypal_called"] is False and made == []
    assert first["steps"] == ["Log in to your own account."] and first["reasons"][0]["reason"] == "bank details changed"
    second = guard.checked_create_order("Hosting renewal", 240, _QUIET["need"], _QUIET["terms"], verified="I logged in to my own account.")
    assert second["status"] == "ORDER_CREATED" and made == ["secondlook-N2"] and sent[1]["verified"] == "I logged in to my own account."


def test_page_has_the_quiet_invoice_example_and_the_verify_box():
    r = handler.lambda_handler({"requestContext": {"http": {"method": "GET", "path": "/"}}, "rawPath": "/"}, None)
    for needle in ("Load quiet invoice example", 'id="verifyBox"', "VERIFY FIRST", 'id="recheck"', "badge-verify"):
        assert needle in r["body"], needle


def test_a_lone_doubt_goes_to_a_larger_crowd_and_a_hold_never_does():
    ver = _fn('{"verdict":"verify","reason":"new payee","quote":null,"step":"Call them."}')
    clr = _fn('{"verdict":"clear","reason":"ok","quote":null}')
    hold = _fn('{"verdict":"hold","reason":"scam","quote":null}')
    calls = []

    def extra(answer):
        def fn(messages):
            calls.append(answer); return answer
        return fn
    c = '{"verdict":"clear","reason":"ordinary","quote":null}'
    v = '{"verdict":"verify","reason":"unchecked request","quote":null,"step":"Log in to your own account."}'
    # one doubt, larger crowd does not share it -> allowed, and it says so
    d = crowd.judge_purchase(dict(_QUIET), [clr, ver, clr], min_clear=3, extra_fns=[extra(c), extra(c)])
    assert d["escalated"] and d["decision"] == "allow" and d["cleared"] and len(d["verdicts"]) == 5 and d["clear_count"] == 4
    assert "larger crowd" in d["summary"] and d["verify_steps"] == []
    # one doubt, shared by one of the two added reviewers -> locked, both steps offered
    d = crowd.judge_purchase(dict(_QUIET), [clr, ver, clr], min_clear=3, extra_fns=[extra(c), extra(v)])
    assert d["escalated"] and d["decision"] == "verify" and not d["cleared"] and d["verify_steps"] == ["Call them.", "Log in to your own account."]
    # an added reviewer that holds stops the payment
    d = crowd.judge_purchase(dict(_QUIET), [clr, ver, clr], min_clear=3, extra_fns=[extra(c), hold])
    assert d["decision"] == "hold" and not d["cleared"]
    # two doubts, two holds or a unanimous clear need no larger crowd
    calls.clear()
    assert crowd.judge_purchase(dict(_QUIET), [ver, ver, clr], min_clear=3, extra_fns=[extra(c), extra(c)])["decision"] == "verify"
    assert crowd.judge_purchase(dict(_QUIET), [hold, hold, clr], min_clear=3, extra_fns=[extra(c), extra(c)])["decision"] == "hold"
    assert crowd.judge_purchase(dict(_QUIET), [hold, ver, clr], min_clear=3, extra_fns=[extra(c), extra(c)])["decision"] == "hold"
    assert crowd.judge_purchase(dict(_QUIET), [clr, clr, clr], min_clear=3, extra_fns=[extra(c), extra(c)])["escalated"] is False
    assert calls == []


def test_a_lone_hold_is_confirmed_by_the_larger_crowd_or_becomes_verify_first_never_allow():
    clr = _fn('{"verdict":"clear","reason":"ok","quote":null}')
    hold = _fn('{"verdict":"hold","reason":"looks like an advance fee","quote":null}')
    ver = _fn('{"verdict":"verify","reason":"unchecked","quote":null,"step":"Call the venue on its listed number."}')
    # nobody else sees a scam: the payment is still locked, as a check, with the objection shown
    d = crowd.judge_purchase(dict(_QUIET), [hold, clr, clr], min_clear=3, extra_fns=[clr, clr])
    assert d["escalated"] and d["decision"] == "verify" and d["cleared"] is False and d["objection_not_confirmed"] is True
    assert "did not confirm" in d["summary"] and "advance fee" in d["summary"] and d["verify_steps"]
    d = crowd.judge_purchase(dict(_QUIET), [clr, clr, hold], min_clear=3, extra_fns=[clr, ver])
    assert d["decision"] == "verify" and d["verify_steps"] == ["Call the venue on its listed number."]
    # a second reviewer in the larger crowd also holds: it is a hold
    d = crowd.judge_purchase(dict(_QUIET), [hold, clr, clr], min_clear=3, extra_fns=[clr, hold])
    assert d["decision"] == "hold" and d["objection_not_confirmed"] is False
    # without a larger crowd configured, one hold stops the payment as before
    assert crowd.judge_purchase(dict(_QUIET), [hold, clr, clr], min_clear=3)["decision"] == "hold"


def test_a_garbled_answer_is_asked_again_once():
    n = {"k": 0}

    def flaky(messages):
        n["k"] += 1
        return "sorry, here is my thinking..." if n["k"] == 1 else '{"verdict":"clear","reason":"ok","quote":null}'
    d = crowd.judge_purchase(dict(_QUIET), [flaky], min_clear=1)
    assert n["k"] == 2 and d["decision"] == "allow"
    d = crowd.judge_purchase(dict(_QUIET), [_fn("still not json")], min_clear=1)
    assert d["decision"] == "hold" and d["verdicts"][0]["reason"] == "unparseable answer"


def test_a_silent_added_reviewer_leaves_the_doubt_standing_not_a_hold():
    ver = _fn('{"verdict":"verify","reason":"new payee","quote":null,"step":"Call them."}')
    clr = _fn('{"verdict":"clear","reason":"ok","quote":null}')
    d = crowd.judge_purchase(dict(_QUIET), [clr, ver, clr], min_clear=3, extra_fns=[clr, _fn("")])
    assert d["decision"] == "verify" and d["verdicts"][4]["verdict"] == "verify" and "doubt stands" in d["verdicts"][4]["reason"]


# ---- how the request reached the buyer ----
def test_a_request_that_came_to_the_buyer_is_never_cleared_unchecked_even_if_all_reviewers_clear():
    clr = _fn('{"verdict":"clear","reason":"looks like a normal renewal","quote":null}')
    calls = []
    extra = lambda m: (calls.append(1), '{"verdict":"clear","reason":"ok","quote":null}')[1]
    p = {"item": "Domain renewal", "price": "199.00", "currency": "USD", "need": "I need to renew my domain to keep my website online.",
         "terms": "Renew your domain now to avoid any downtime.", "origin": "contacted"}
    d = crowd.judge_purchase(dict(p), [clr, clr, clr], min_clear=3, extra_fns=[extra, extra])
    assert d["decision"] == "verify" and d["cleared"] is False and d["unchecked_inbound"] is True
    assert d["verify_steps"] and "Do not use the link" in d["verify_steps"][0] and calls == []
    # the buyer went there themselves: the same text is an ordinary purchase
    assert crowd.judge_purchase(dict(p, origin="self"), [clr, clr, clr], min_clear=3)["decision"] == "allow"
    # the buyer checked: now the reviewers decide
    d = crowd.judge_purchase(dict(p, verified="I logged in at my registrar and the renewal is there."), [clr, clr, clr], min_clear=3)
    assert d["decision"] == "allow" and d["unchecked_inbound"] is False
    # a hold still wins
    hold = _fn('{"verdict":"hold","reason":"fake renewal","quote":null}')
    assert crowd.judge_purchase(dict(p), [clr, hold, clr], min_clear=3)["decision"] == "hold"


def test_origin_reaches_the_reviewers_is_validated_and_is_part_of_the_clearance(monkeypatch):
    seen = []
    fn = lambda m: (seen.append(m[-1]["content"]), '{"verdict":"clear","reason":"ok","quote":null}')[1]
    crowd.judge_purchase(dict(_QUIET, origin="contacted", source="They emailed me an invoice."), [fn], min_clear=1)
    assert "How it reached the buyer: CONTACTED: the request came to the buyer. They emailed me an invoice." in seen[0]
    seen.clear(); crowd.judge_purchase(dict(_QUIET), [fn], min_clear=1)
    assert "How it reached the buyer" not in seen[0]
    assert handler._validate_purchase(dict(_QUIET, origin="somewhere"))[0] is False
    assert handler._validate_purchase(dict(_QUIET, origin="self", source="s" * 501))[0] is False
    assert handler._validate_purchase(dict(_QUIET, origin="contacted", source="email"))[0] is True
    monkeypatch.setenv("SECONDLOOK_TOKEN_SEED", "t")
    tok = handler._sign_clearance(dict(_QUIET, origin="self"))
    assert handler._verify_clearance(dict(_QUIET, origin="self"), tok)
    assert handler._verify_clearance(dict(_QUIET, origin="contacted"), tok) is None and handler._verify_clearance(dict(_QUIET), tok) is None


def test_page_asks_how_the_request_reached_the_buyer_and_the_agent_tool_requires_it():
    r = handler.lambda_handler({"requestContext": {"http": {"method": "GET", "path": "/"}}, "rawPath": "/"}, None)
    assert 'id="origin"' in r["body"] and "How did this payment request reach you?" in r["body"] and "p.origin = o" in r["body"]
    from agent import shopping_agent
    props = shopping_agent.TOOL["function"]["parameters"]
    assert "request_came_to_user" in props["required"] and props["properties"]["request_came_to_user"]["type"] == "boolean"
    assert shopping_agent.origin_of({"request_came_to_user": False}) == "self" and shopping_agent.origin_of({"request_came_to_user": True}) == "contacted"
    assert shopping_agent.origin_of({}) == "contacted" and shopping_agent.origin_of({"asked_by_someone_they_know": True}) == "known_person"
    assert "never answer the check yourself" in shopping_agent.SYSTEM and len(shopping_agent.TASKS) == 3
    assert "Never write this yourself" in props["properties"]["verified"]["description"]
    assert handler._OPENAPI["components"]["schemas"]["Purchase"]["properties"]["origin"]["enum"] == ["self", "contacted", "known_person"]


def test_paypal_rate_limit_is_waited_out_with_the_same_request_id_and_gives_up_after_three():
    import io
    import urllib.error

    class Limited:
        def __init__(self, fails, retry_after=None):
            self.fails, self.requests, self.retry_after = fails, [], retry_after

        def open(self, request, timeout=None):
            self.requests.append(request)
            if "oauth2" in request.full_url:
                return FakeResponse(json.dumps({"access_token": "t"}).encode())
            if self.fails > 0:
                self.fails -= 1
                hdrs = {"Retry-After": self.retry_after} if self.retry_after else {}
                raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", hdrs, io.BytesIO(b"{}"))
            return FakeResponse(json.dumps({"id": "O1", "status": "CREATED", "links": []}).encode())

    waits = []
    op = Limited(2)
    sb = paypal.PayPalSandbox("id", "secret", opener=op, sleep=waits.append)
    out = sb.create_order({"item": "Kettle", "price": "30.00", "currency": "USD"}, "cleared", request_id="secondlook-abc")
    assert out["id"] == "O1" and waits == [0.5, 1.0] and sb.retries == 2
    ids = [r.get_header("Paypal-request-id") for r in op.requests if "orders" in r.full_url]
    assert ids == ["secondlook-abc"] * 3, "every retry must carry the same request id, so PayPal makes one order"

    waits = []
    sb = paypal.PayPalSandbox("id", "secret", opener=Limited(1, retry_after="3"), sleep=waits.append)
    sb.create_order({"item": "Kettle", "price": "30.00", "currency": "USD"}, "cleared", request_id="secondlook-abc")
    assert waits == [3.0], "PayPal's Retry-After is honoured"

    waits = []
    sb = paypal.PayPalSandbox("id", "secret", opener=Limited(99), sleep=waits.append)
    try:
        sb.create_order({"item": "Kettle", "price": "30.00", "currency": "USD"}, "cleared", request_id="secondlook-abc")
        assert False, "should have given up"
    except paypal.SecondLookError as e:
        assert "429" in str(e)
    assert waits == [0.5, 1.0, 2.0]


def test_agent_that_does_not_say_how_the_request_arrived_is_treated_as_inbound_and_the_users_check_is_passed_on(monkeypatch):
    from agent import shopping_agent
    seen = []

    class G:
        def checked_create_order(self, item, price, need, terms, origin=None, verified=None):
            seen.append((origin, verified))
            return {"status": "VERIFY_FIRST", "paypal_called": False} if not verified else {"status": "ORDER_CREATED", "paypal_called": True}

    def call(args):
        return {"tool_calls": [{"id": "c1", "function": {"arguments": json.dumps(args)}}]}

    base = {"item": "Hosting", "price": 240, "need": "n", "terms": "t"}
    replies = [call(dict(base, self="self")), {"content": "please check"},
               call(dict(base, request_came_to_user=True, verified="I logged in myself")), {"content": "paid"}]
    monkeypatch.setattr(shopping_agent, "chat", lambda messages, tools=None: replies.pop(0))
    out = shopping_agent.run_task(G(), "t", "pay this invoice", "I logged in myself")
    assert seen == [("contacted", None), ("contacted", "I logged in myself")] and out["status"] == "ORDER_CREATED"
