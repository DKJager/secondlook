"""Crowd evaluation logic for Second Look.

Runs multiple independent LLM reviewers in parallel and aggregates their verdicts.
"""

import json
import os
import re
import threading
from typing import List, Dict, Any, Callable


_REFUSAL = re.compile(r"disallowed|not allowed|cannot (process|assist|book|complete)|can.t (process|assist|book|complete)|(not able|unable) to (help|assist|process)", re.I)


_ORIGINS = {
    "self": "OWN initiative: the buyer went to the seller themselves.",
    "contacted": "CONTACTED: the request came to the buyer.",
    "known_person": "A person the buyer knows asked them to pay.",
}
_DEFAULT_STEP = ("Open the company's own website or app yourself, or call a number you already had, and look for this charge there. "
                 "Do not use the link, number or reply address in the message.")


class SecondLookError(RuntimeError):
    """Error raised during crowd evaluation."""
    pass


def _parse_verdict(raw_answer: str, terms: str) -> Dict[str, Any]:
    """Parse a reviewer's JSON answer, handling fences and validation.
    
    Args:
        raw_answer: Raw string response from the reviewer.
        terms: The purchase terms to validate quotes against.
        
    Returns:
        Dictionary with "verdict", "reason", and "quote" keys.
    """
    # Strip markdown code fences
    stripped = raw_answer.strip()
    if stripped.startswith("```"):
        # Remove opening fence (possibly with language tag)
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline:].strip()
        # Remove closing fence
        if stripped.endswith("```"):
            stripped = stripped[:-3].strip()
    
    # Try direct JSON parse first
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    
    # If direct parse failed, try to extract the first balanced JSON object
    if parsed is None:
        # Find all { ... } blocks using a non-greedy regex
        matches = list(re.finditer(r'\{.*?\}', stripped, re.DOTALL))
        extracted = None
        for match in matches:
            try:
                extracted = json.loads(match.group(0))
                break
            except (json.JSONDecodeError, ValueError):
                # Try progressively longer matches anchored at the same start
                start = match.start()
                for end in range(match.end(), len(stripped) + 1):
                    candidate = stripped[start:end]
                    if candidate.count('{') != candidate.count('}'):
                        continue
                    try:
                        extracted = json.loads(candidate)
                        break
                    except (json.JSONDecodeError, ValueError):
                        continue
                if extracted is not None:
                    break
        parsed = extracted
    
    # If still no parsed object, return unparseable
    if parsed is None:
        return {
            "verdict": "hold",
            "reason": "unparseable answer",
            "quote": None
        }
    
    # Extract values with case-insensitive key matching
    normalized = {}
    for key, value in parsed.items():
        normalized[key.lower()] = value
    
    verdict_raw = str(normalized.get("verdict", "hold")).lower()
    reason = str(normalized.get("reason", ""))
    quote = normalized.get("quote")
    
    # Determine verdict: "clear" or "hold" based on substring match
    if "verify" in verdict_raw:
        verdict = "verify"
    elif "clear" in verdict_raw:
        verdict = "clear"
    elif "hold" in verdict_raw:
        verdict = "hold"
    else:
        verdict = "hold"
        if reason:
            reason = reason + " (invalid verdict)"
        else:
            reason = "invalid verdict"
    
    # Validate quote - must be a case-insensitive substring of terms
    if quote is not None:
        # Convert both to lowercase for case-insensitive check
        if not isinstance(quote, str) or quote.lower() not in terms.lower():
            quote = None
            if reason:
                reason = reason + " (quote not found in terms)"
            else:
                reason = "quote not found in terms"
    
    step = normalized.get("step")
    step = step.strip()[:240] if isinstance(step, str) and step.strip() and verdict == "verify" else None
    return {
        "verdict": verdict,
        "reason": reason if reason else "",
        "quote": quote,
        "step": step,
    }


def _call_reviewer(
    ask_fn: Callable[[List[Dict[str, str]]], str],
    purchase: Dict[str, str],
    index: int,
    result_list: List[Dict[str, Any]],
    result_lock: threading.Lock,
) -> None:
    """Call a single reviewer function and store the result.
    
    Args:
        ask_fn: The reviewer callable.
        purchase: The purchase data.
        index: Index of this reviewer.
        result_list: Shared list to store results.
        result_lock: Mutex for thread-safe list access.
    """
    system_prompt = (
        "You are one independent reviewer in a family scam guard that runs before a payment. "
        "You do not make the purchase; you only judge whether it is safe to let it proceed. Never refuse to answer. "
        "There are three answers: \"hold\", \"verify\" and \"clear\". "
        "HOLD when the buyer's stated need or the seller's terms show a scam signal: "
        "payment by gift cards, wire, crypto, prepaid cards or gold for a debt, fine, bail, fee, tax or prize; "
        "urgency or secrecy; someone claiming to be family in trouble, police, a government agency, a bank, "
        "a utility, a boss or tech support; an online contact the buyer has not met asking for money; "
        "guaranteed investment returns; paying a fee up front to receive money, a job, a prize, a pet or a rental not yet seen; "
        "sending back an overpayment. Also hold when the seller's terms are clearly unfair for this purchase. "
        "VERIFY when nothing is an outright scam signal, but the buyer is paying because a message reached THEM "
        "(an invoice, bill, renewal or expiry notice, email, text, letter, call or payment link they received, or an offer or invitation they did not seek out) "
        "and at least one of these is true: the payment details, bank account or payee have changed or are new; "
        "the price is new, higher or not what the buyer expected; the buyer is told to pay through a link, portal or contact details that came inside the message; "
        "the buyer is not sure who sent it (\"looks like\", \"official-looking\", \"claims to be\"); there is a deadline or a threat of interruption, loss or a missed slot; "
        "a fee is asked to unlock a discount, refund, acceptance, publication, membership, application or listing; "
        "or the request is unexpected and the buyer names no account, order or contract of theirs that it belongs to. "
        "Also VERIFY when the buyer says they found a private seller or provider online, has not dealt with them before, and is asked to pay in cash or in full before seeing the goods or the work. "
        "Paying someone the buyer already knows, employs, hired or has received work from is ordinary, and so is sending money to the buyer's own family member "
        "for a reason the buyer gives themselves (tuition, housing, a gift, support). A shop's or marketplace's normal time limit to complete a purchase the buyer chose is not urgency. "
        "Verify is not an accusation: most such requests are real. It means check first through a channel the buyer already had. "
        "CLEAR for an ordinary purchase the buyer chose to make, with an ordinary reason, even if it is a gift card, crypto, "
        "a final sale, a donation or an urgent repair, when no scam signal is present. "
        "CLEAR an expected, regular bill for the usual amount paid the usual way. "
        "CLEAR a bill or invoice when the buyer says they logged in to their own account, called a number they already had, "
        "matched it to their own order, contract or usual amount, or otherwise checked it without using the message's own links or contacts. "
        "A line 'How it reached the buyer' may be present. If it says the buyer was CONTACTED (the request came to them: an email, text, call, letter, "
        "invoice, notice, advert or link sent to them) and there is no line 'Buyer's verification', answer \"verify\" however ordinary the request looks, "
        "unless it is a hold; a real bill and a fake one read the same, so the buyer must look for themselves. "
        "If it says the buyer went to the seller on their OWN initiative, an ordinary purchase is clear. "
        "Urgency or a family emergency alone is not a scam signal: a buyer paying a business directly for its own "
        "product or service (an airline, a clinic, a repair service, a contractor they hired under a contract) is ordinary. "
        "The signal is someone else telling the buyer to pay in an unusual way, money going to a person the buyer cannot verify, "
        "or a payment request the buyer has not checked for themselves. "
        "If a line 'Buyer's verification' is present, judge it: it counts only if it is independent of the message "
        "(their own account or app, a number or address from an old bill or the official site they typed in, a person they already know). "
        "Seeing the goods, the person or the paperwork in person also counts, and so does paying the bill inside the buyer's own account or app as they always do. "
        "For a regular bill the buyer expected, it is also enough that they say it is the usual amount, paid the usual way to the same payee as always, "
        "as long as nothing about the payment details has changed. "
        "The check must confirm THIS charge or THESE payment details with the real company or person. "
        "Replying to the message, clicking its link, calling the number in it, saying it looked official, or recognising a name, logo, "
        "account number or invoice number printed on the request does not count, because a fake carries those too: answer \"verify\" again. "
        "Your answer must be ONLY a single JSON object on one line. No markdown, no reasoning, no code fences. "
        "Example: {\"verdict\":\"verify\",\"reason\":\"...\",\"quote\":\"...\",\"step\":\"...\"}. "
        "Use verdict \"clear\", \"verify\" or \"hold\"; reason <=30 words and, on a hold, name the scam pattern; "
        "quote must be copied word for word from the Need or the Terms (the words that decided it), or null; "
        "step is null unless the verdict is verify, then it is ONE concrete check under 25 words, specific to this request, that the buyer can do now "
        "without using anything in the message."
    )
    user_message = (
        f"Item: {purchase['item']}\n"
        f"Price: {purchase['price']}\n"
        f"Currency: {purchase['currency']}\n"
        f"Terms: {purchase['terms']}\n"
        f"Need: {purchase['need']}"
    )
    origin = str(purchase.get("origin") or "").strip().lower()
    if origin in _ORIGINS or str(purchase.get("source") or "").strip():
        user_message += "\nHow it reached the buyer: " + (_ORIGINS.get(origin, "") + " " + str(purchase.get("source") or "").strip()).strip()
    if str(purchase.get("verified") or "").strip():
        user_message += f"\nBuyer's verification: {str(purchase['verified']).strip()}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    
    try:
        raw_answer = ask_fn(messages)
        if isinstance(raw_answer, str) and _REFUSAL.search(raw_answer):
            raw_answer = ask_fn(messages + [
                {"role": "assistant", "content": raw_answer[:300]},
                {"role": "user", "content": "You are not being asked to make or process a purchase. "
                 "Review it for scam signals only and answer with the JSON object."},
            ])
    except Exception as exc:
        # ask_fn itself raised an exception – model error
        exc_text = str(exc)
        # Strip URLs that might contain keys; keep first 120 chars
        exc_text = re.sub(r'https?://\S+', '[URL]', exc_text)
        reason = "model error: " + exc_text[:120]
        parsed = {
            "verdict": "hold",
            "reason": reason,
            "quote": None
        }
        raw = ""
    else:
        # Successfully got an answer, try to parse JSON
        source_text = purchase["terms"] + "\n" + purchase["need"] + "\n" + str(purchase.get("verified") or "") + "\n" + str(purchase.get("source") or "")
        try:
            parsed = _parse_verdict(raw_answer, source_text)
        except Exception:
            parsed = {"verdict": "hold", "reason": "unparseable answer", "quote": None}
        if parsed.get("reason") == "unparseable answer":
            # A garbled answer is a fault, not an objection: ask once more before counting it as a hold.
            try:
                raw_answer = ask_fn(messages)
                parsed = _parse_verdict(raw_answer, source_text)
            except Exception:
                parsed = {"verdict": "hold", "reason": "unparseable answer", "quote": None}
        raw = (raw_answer or "")[:300] if isinstance(raw_answer, str) else ""
    
    q = parsed.get("quote")
    quote_from = None
    if isinstance(q, str):
        quote_from = "need" if q.lower() in purchase["need"].lower() else "terms"
    result = {
        "quote_from": quote_from,
        "model_index": index,
        "verdict": parsed["verdict"],
        "reason": parsed["reason"],
        "quote": parsed["quote"],
        "step": parsed.get("step"),
        "raw": raw,
    }
    
    with result_lock:
        result_list.append(result)


def judge_purchase(
    purchase: Dict[str, str],
    ask_fns: List[Callable[[List[Dict[str, str]]], str]],
    min_clear: int = int(os.getenv("SECONDLOOK_MIN_CLEAR", "3")),
    extra_fns: List[Callable[[List[Dict[str, str]]], str]] = None,
) -> Dict[str, Any]:
    """Judge a purchase using multiple reviewer functions.

    Args:
        purchase: Mapping with keys "item", "price", "currency", "terms", "need".
        ask_fns: Callables that accept a list of messages and return a JSON string.
        min_clear: Minimum number of "clear" verdicts required for overall clearance.

    Returns:
        Dictionary containing individual verdicts, clear count, cleared flag,
        and a human‑readable summary.

    Raises:
        SecondLookError: If any unexpected error occurs during processing.
    """
    # Validate purchase keys
    required_keys = {"item", "price", "currency", "terms", "need"}
    if not required_keys.issubset(purchase.keys()):
        raise SecondLookError(
            f"Purchase must contain keys: {required_keys}"
        )
    
    # Run all reviewers in parallel
    verdicts: List[Dict[str, Any]] = []
    verdicts_lock = threading.Lock()
    threads = []
    
    for i, ask_fn in enumerate(ask_fns):
        thread = threading.Thread(
            target=_call_reviewer,
            args=(ask_fn, purchase, i, verdicts, verdicts_lock),
        )
        thread.start()
        threads.append(thread)
    
    for thread in threads:
        thread.join()
    
    # A reviewer whose thread produced nothing counts as a hold, never as absent
    seen = {v["model_index"] for v in verdicts}
    for i in range(len(ask_fns)):
        if i not in seen:
            verdicts.append({"model_index": i, "verdict": "hold", "reason": "no answer from reviewer", "quote": None, "raw": ""})
    # Sort by model_index to maintain order
    verdicts.sort(key=lambda v: v["model_index"])
    
    # One reviewer alone against two clears is never ignored and never decides alone: the question goes to a
    # larger crowd (more makers).
    #   lone "verify": locked for a check if any added reviewer shares the doubt; cleared if none does.
    #   lone "hold":   stays a hold if any added reviewer also holds; otherwise it becomes "verify first".
    #                  A purchase that any reviewer held is never paid before the buyer has checked.
    escalated = False
    first = [v["verdict"] for v in verdicts]
    unchecked_inbound = str(purchase.get("origin") or "").strip().lower() == "contacted" and not str(purchase.get("verified") or "").strip()
    lone = [x for x in first if x != "clear"]
    lone_hold = False
    if extra_fns and not unchecked_inbound and len(lone) == 1 and len(first) >= min_clear:
        lone_hold = lone[0] == "hold"
        escalated = True
        more: List[Dict[str, Any]] = []
        more_threads = []
        for j, ask_fn in enumerate(extra_fns):
            t = threading.Thread(target=_call_reviewer, args=(ask_fn, purchase, len(ask_fns) + j, more, verdicts_lock))
            t.start(); more_threads.append(t)
        for t in more_threads:
            t.join()
        got = {v["model_index"] for v in more}
        for j in range(len(extra_fns)):
            if len(ask_fns) + j not in got:
                more.append({"model_index": len(ask_fns) + j, "verdict": "hold", "reason": "no answer from reviewer", "quote": None, "raw": ""})
        for v in more:
            # An added reviewer that gives no usable answer has not removed the doubt: the check stands.
            if v["verdict"] == "hold" and (v["reason"] in ("unparseable answer", "no answer from reviewer") or v["reason"].startswith("model error")):
                v["verdict"] = "verify"
                v["reason"] = "no usable answer from this reviewer, so the doubt stands"
        more.sort(key=lambda v: v["model_index"])
        verdicts.extend(more)

    # Count clear verdicts
    clear_count = sum(1 for v in verdicts if v["verdict"] == "clear")
    total = len(verdicts)
    
    # Build summary
    hold_reasons = [v["reason"] for v in verdicts if v["verdict"] == "hold"]
    verify_reasons = [v["reason"] for v in verdicts if v["verdict"] == "verify"]
    added = verdicts[len(ask_fns):] if escalated else []
    downgraded = False
    if escalated and lone_hold:
        cleared = False
        downgraded = not any(v["verdict"] == "hold" for v in added)   # nobody in the larger crowd confirmed a scam
    elif escalated:
        # cleared only if every added reviewer cleared; then exactly one reviewer of the whole crowd had a doubt
        cleared = not hold_reasons and len(verify_reasons) == 1 and all(v["verdict"] == "clear" for v in added)
    else:
        cleared = clear_count >= min_clear
    # A confirmed hold stops the payment. Otherwise a doubt locks it until the buyer has checked for themselves.
    decision = "allow" if cleared else ("verify" if downgraded else ("hold" if hold_reasons else ("verify" if verify_reasons else "hold")))
    # Floor, in code and not left to a model: a request that reached the buyer is never paid before the buyer has
    # looked for themselves. A real bill and a fake one can read exactly the same.
    floor = False
    if unchecked_inbound and decision == "allow":
        decision, cleared, floor = "verify", False, True
    steps = []
    for v in verdicts:
        if v["verdict"] == "verify" and v.get("step") and v["step"] not in steps:
            steps.append(v["step"])
    if decision == "verify" and not steps:
        steps.append("Before paying, check this with the company or person through a channel you already trust, or ask someone you trust to look at it with you."
                     if downgraded else _DEFAULT_STEP)
    if downgraded:
        summary = f"{clear_count}/{total} clear; one reviewer objected and the larger crowd did not confirm a scam, so check first: {'; '.join(hold_reasons)}"
    elif floor:
        summary = f"{clear_count}/{total} clear; verify first: this request came to the buyer and has not been checked by them"
    elif hold_reasons:
        summary = f"{clear_count}/{total} clear; holds: {'; '.join(hold_reasons)}"
    elif verify_reasons and cleared:
        summary = f"{clear_count}/{total} clear; one reviewer's doubt was put to a larger crowd and not shared"
    elif verify_reasons:
        summary = f"{clear_count}/{total} clear; verify first: {'; '.join(verify_reasons)}"
    else:
        summary = f"{clear_count}/{total} clear"

    return {
        "verdicts": verdicts,
        "clear_count": clear_count,
        "cleared": cleared,
        "decision": decision,
        "escalated": escalated,
        "unchecked_inbound": floor,
        "objection_not_confirmed": downgraded,
        "verify_steps": steps if decision == "verify" else [],
        "summary": summary,
    }