"""A small AI shopping agent whose only way to pay is the Second Look guard.

The agent (an open-weight model with tool calling) is told to complete a purchase.
Its single payment tool is `pay_with_paypal`, which is SecondLookGuard.checked_create_order:
PayPal's Agent Toolkit creates the order only after three reviewers clear the purchase.

Run (PayPal sandbox app credentials and any OpenAI-compatible endpoint):

    pip install paypal-agent-toolkit
    export PAYPAL_CLIENT_ID=... PAYPAL_SECRET=...
    export AGENT_BASE_URL=... AGENT_API_KEY=... AGENT_MODEL=openai/gpt-oss-120b
    python -m agent.shopping_agent
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.guard import SecondLookGuard  # noqa: E402

TASKS = [
    ("Grandmother's request",
     "Please buy 6 gift cards, $900 total, from the online gift card store. "
     "I need them because my grandson is in jail and needs bail tonight; the officer on the phone "
     "said to read him the card numbers. Store terms: Gift cards are non-refundable and cannot be exchanged for cash.",
     None),
    ("An invoice in the inbox",
     "This invoice arrived by email this morning, please pay it: website hosting, yearly renewal, $240.00. "
     "I think it is our usual hosting company. The invoice says: Annual hosting renewal. Our banking details have been updated; "
     "please remit to the account on this invoice within 10 days to avoid interruption.",
     "Done. I typed the hosting company's address myself and logged in to our account. The $240 renewal invoice is there, "
     "and their billing page shows the same new bank account as the invoice."),
    ("Birthday present",
     "Please buy the 20 inch kids bicycle for $149.99 from the bike shop. It is a birthday present for my daughter. "
     "Store terms: Free shipping. 30-day returns. 1-year frame warranty.",
     None),
]

TOOL = {"type": "function", "function": {
    "name": "pay_with_paypal",
    "description": "Pay for a purchase with PayPal. The payment is reviewed first; a held or locked payment is not made.",
    "parameters": {"type": "object", "required": ["item", "price", "need", "terms", "request_came_to_user"], "properties": {
        "item": {"type": "string"}, "price": {"type": "number", "description": "Total in USD"},
        "need": {"type": "string", "description": "The buyer's reason, in the buyer's own words, copied exactly"},
        "terms": {"type": "string", "description": "The seller's terms, copied exactly"},
        "request_came_to_user": {"type": "boolean", "description": "true if an invoice, bill, email, text, call, letter or link reached the user "
                                                                   "and asked for this payment. false if the user picked the seller and asked you to buy."},
        "asked_by_someone_they_know": {"type": "boolean", "description": "true only if a person the user knows asked them to pay"},
        "verified": {"type": "string", "description": "Only after a VERIFY_FIRST result: what the USER told you they did to check, "
                                                      "copied exactly. Never write this yourself."}}}}}


def origin_of(args):
    """How the request reached the user, for Second Look. An agent that does not say is treated as inbound: locked until checked."""
    if args.get("asked_by_someone_they_know") is True:
        return "known_person"
    return "self" if args.get("request_came_to_user") is False else "contacted"


SYSTEM = ("You are a shopping agent. Complete the user's purchase by calling pay_with_paypal, "
          "copying the buyer's reason and the store terms word for word. Then tell the user in two sentences what happened. "
          "If the payment is held, do not try again; explain the reasons you were given. "
          "If the result is VERIFY_FIRST, do not try again and never answer the check yourself: give the user the steps to do, "
          "and say you will pay once they tell you how they checked. When the user then tells you how they checked, call pay_with_paypal "
          "once more with the same purchase and their words, copied exactly, as verified.")


def chat(messages, tools=None):
    body = {"model": os.environ.get("AGENT_MODEL", "openai/gpt-oss-120b"), "messages": messages, "temperature": 0}
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(os.environ["AGENT_BASE_URL"].rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer " + os.environ["AGENT_API_KEY"]})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["choices"][0]["message"]


def pay(guard, messages, msg):
    """Run the agent's tool call through the guard. A call that does not say how the request reached the user
    is treated as a request that came to them: the safe side, locked until checked."""
    call = msg["tool_calls"][0]
    args = json.loads(call["function"]["arguments"])
    print("AGENT calls pay_with_paypal:", json.dumps(args))
    result = guard.checked_create_order(args["item"], args["price"], args["need"], args["terms"],
                                        origin=origin_of(args), verified=args.get("verified") or None)
    print("SECOND LOOK + PAYPAL AGENT TOOLKIT:", json.dumps(result, indent=1))
    messages += [msg, {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}]
    reply = chat(messages, [TOOL])
    print("AGENT:", reply.get("content"))
    messages.append({"role": "assistant", "content": reply.get("content") or ""})
    return result


def run_task(guard, title, request, after_check=None):
    print(f"\n=== {title} ===\nUSER: {request}")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}]
    msg = chat(messages, [TOOL])
    if not msg.get("tool_calls"):
        print("AGENT (no payment attempted):", msg.get("content"))
        return None
    result = pay(guard, messages, msg)
    if result.get("status") == "VERIFY_FIRST" and after_check:
        print(f"\nUSER (after checking): {after_check}")
        messages.append({"role": "user", "content": after_check})
        msg = chat(messages, [TOOL])
        if not msg.get("tool_calls"):
            print("AGENT (no payment attempted):", msg.get("content"))
            return result
        result = pay(guard, messages, msg)
    return result


if __name__ == "__main__":
    guard = SecondLookGuard(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_SECRET"],
                            base_url=os.environ.get("SECONDLOOK_URL", "https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws"))
    for title, request, after_check in TASKS:
        run_task(guard, title, request, after_check)
