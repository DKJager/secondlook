"""Second Look guard for the PayPal Agent Toolkit.

Wrap the toolkit so an AI agent's `create_order` goes through Second Look first.
The agent is given `checked_create_order` instead of the raw toolkit tool: a purchase
that any reviewer holds never reaches PayPal, and the agent gets the reasons back.

    from paypal_agent_toolkit.shared.api import PayPalAPI
    from paypal_agent_toolkit.shared.configuration import Context
    from agent.guard import SecondLookGuard

    guard = SecondLookGuard(client_id, secret)
    result = guard.checked_create_order(item="Kids bicycle", price=149.99,
                                        need="Birthday present for my daughter",
                                        terms="Free shipping. 30-day returns.")
"""
import json
import urllib.request

DEFAULT_BASE = "https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws"


class SecondLookGuard:
    def __init__(self, client_id, secret, base_url=DEFAULT_BASE, sandbox=True, paypal_api_factory=None):
        self.client_id = client_id
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.sandbox = sandbox
        self._factory = paypal_api_factory or self._toolkit_api

    def _toolkit_api(self, request_id):
        from paypal_agent_toolkit.shared.api import PayPalAPI
        from paypal_agent_toolkit.shared.configuration import Context
        return PayPalAPI(self.client_id, self.secret, Context(sandbox=self.sandbox, request_id=request_id))

    def check(self, purchase):
        """Ask Second Look. Returns its decision dict (decision, reviews, clearance_token)."""
        body = json.dumps({"purchase": purchase}).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/agent/check", data=body,
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())

    def checked_create_order(self, item, price, need, terms, currency="USD", verified=None):
        """Create a PayPal order through the Agent Toolkit only if Second Look allows it.

        `verified` is the BUYER's own account of how they checked a payment request, passed on a second call
        after a VERIFY_FIRST result. An agent must get it from its human, not write it itself.
        """
        purchase = {"item": str(item), "price": f"{float(price):.2f}", "currency": currency,
                    "need": str(need), "terms": str(terms)}
        if verified:
            purchase["verified"] = str(verified)
        decision = self.check(purchase)
        if decision.get("decision") == "verify":
            return {"status": "VERIFY_FIRST", "paypal_called": False, "steps": decision.get("verify_steps", []),
                    "reasons": [{"reviewer": r.get("model"), "reason": r.get("reason"), "quote": r.get("quote")}
                                for r in decision.get("reviews", []) if r.get("verdict") == "verify"],
                    "next": "Ask the buyer to do one of the steps themselves, then call again with verified=<how they checked>."}
        if decision.get("decision") != "allow" or not decision.get("clearance_token"):
            return {"status": "HELD", "paypal_called": False,
                    "reasons": [{"reviewer": r.get("model"), "reason": r.get("reason"), "quote": r.get("quote")}
                                for r in decision.get("reviews", []) if r.get("verdict") == "hold"]}
        # One clearance maps to one order: the clearance nonce is the PayPal-Request-Id.
        nonce = decision["clearance_token"].split(".")[1]
        api = self._factory("secondlook-" + nonce)
        raw = api.run("create_order", {
            "currency_code": currency,
            "items": [{"name": purchase["item"][:120], "quantity": 1,
                       "item_cost": float(purchase["price"]), "item_total": float(purchase["price"]),
                       "description": "Second Look: cleared by the reviewer crowd"}],
        })
        order = json.loads(raw) if isinstance(raw, str) else raw
        approve = next((l.get("href") for l in order.get("links", []) if l.get("rel") in ("approve", "payer-action")), None)
        return {"status": "ORDER_CREATED", "paypal_called": True, "order_id": order.get("id"),
                "paypal_status": order.get("status"), "approve_url": approve}
