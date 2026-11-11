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
     "said to read him the card numbers. Store terms: Gift cards are non-refundable and cannot be exchanged for cash."),
    ("An invoice in the inbox",
     "This invoice arrived by email this morning, please pay it: website hosting, yearly renewal, $240.00. "
     "I think it is our usual hosting company. The invoice says: Annual hosting renewal. Our banking details have been updated; "
     "please remit to the account on this invoice within 10 days to avoid interruption."),
    ("Birthday present",
     "Please buy the 20 inch kids bicycle for $149.99 from the bike shop. It is a birthday present for my daughter. "
     "Store terms: Free shipping. 30-day returns. 1-year frame warranty."),
]

TOOL = {"type": "function", "function": {
    "name": "pay_with_paypal",
    "description": "Pay for a purchase with PayPal. The payment is reviewed first; a held payment is not made.",
    "parameters": {"type": "object", "required": ["item", "price", "need", "terms", "origin"], "properties": {
        "origin": {"type": "string", "enum": ["self", "contacted", "known_person"],
                   "description": "self: the user chose this seller and asked you to buy. contacted: the request reached the user "
                                  "(an invoice, email, text, call, letter or link). known_person: someone the user knows asked them to pay"},
        "item": {"type": "string"}, "price": {"type": "number", "description": "Total in USD"},
        "need": {"type": "string", "description": "The buyer's reason, in the buyer's own words, copied exactly"},
        "terms": {"type": "string", "description": "The seller's terms, copied exactly"}}}}}

SYSTEM = ("You are a shopping agent. Complete the user's purchase by calling pay_with_paypal exactly once, "
          "copying the buyer's reason and the store terms word for word. Then tell the user in two sentences what happened. "
          "If the payment is held, do not try again; explain the reasons you were given. "
          "If the result is VERIFY_FIRST, do not try again and never answer the check yourself: give the user the steps to do, "
          "and say you will pay once they tell you how they checked.")


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


def run_task(guard, title, request):
    print(f"\n=== {title} ===\nUSER: {request}")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}]
    msg = chat(messages, [TOOL])
    calls = msg.get("tool_calls") or []
    if not calls:
        print("AGENT (no payment attempted):", msg.get("content"))
        return None
    args = json.loads(calls[0]["function"]["arguments"])
    print("AGENT calls pay_with_paypal:", json.dumps(args))
    result = guard.checked_create_order(args["item"], args["price"], args["need"], args["terms"], origin=args.get("origin"))
    print("SECOND LOOK + PAYPAL AGENT TOOLKIT:", json.dumps(result, indent=1))
    messages += [msg, {"role": "tool", "tool_call_id": calls[0]["id"], "content": json.dumps(result)}]
    print("AGENT:", chat(messages).get("content"))
    return result


if __name__ == "__main__":
    guard = SecondLookGuard(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_SECRET"],
                            base_url=os.environ.get("SECONDLOOK_URL", "https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws"))
    for title, request in TASKS:
        run_task(guard, title, request)
