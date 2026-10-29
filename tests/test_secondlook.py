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
