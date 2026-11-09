"""Crowd evaluation logic for Second Look.

Runs multiple independent LLM reviewers in parallel and aggregates their verdicts.
"""

import json
import os
import re
import threading
from typing import List, Dict, Any, Callable


_REFUSAL = re.compile(r"disallowed|not allowed|cannot (process|assist|book|complete)|can.t (process|assist|book|complete)|(not able|unable) to (help|assist|process)", re.I)


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
    if "clear" in verdict_raw:
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
    
    return {
        "verdict": verdict,
        "reason": reason if reason else "",
        "quote": quote
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
        "Answer \"hold\" when the buyer's stated need or the seller's terms show a scam signal: "
        "payment by gift cards, wire, crypto, prepaid cards or gold for a debt, fine, bail, fee, tax or prize; "
        "urgency or secrecy; someone claiming to be family in trouble, police, a government agency, a bank, "
        "a utility, a boss or tech support; an online contact the buyer has not met asking for money; "
        "guaranteed investment returns; paying a fee up front to receive money, a job, a prize, a pet or a rental not yet seen; "
        "sending back an overpayment. Also hold when the seller's terms are clearly unfair for this purchase. "
        "Answer \"clear\" for an ordinary purchase with an ordinary reason, even if it is a gift card, crypto, "
        "a final sale, a donation or an urgent repair, when no scam signal is present. "
        "Urgency or a family emergency alone is not a scam signal: a buyer paying a business directly for its own "
        "product or service (an airline, a clinic, a repair service, a contractor they hired under a contract) is ordinary. "
        "The signal is someone else telling the buyer to pay in an unusual way, or money going to a person the buyer cannot verify. "
        "Your answer must be ONLY a single JSON object on one line. No markdown, no reasoning, no code fences. "
        "Example: {\"verdict\":\"hold\",\"reason\":\"...\",\"quote\":\"...\"}. "
        "Use verdict \"clear\" or \"hold\"; reason <=30 words and, on a hold, name the scam pattern; "
        "quote must be copied word for word from the Need or the Terms (the words that decided it), or null."
    )
    user_message = (
        f"Item: {purchase['item']}\n"
        f"Price: {purchase['price']}\n"
        f"Currency: {purchase['currency']}\n"
        f"Terms: {purchase['terms']}\n"
        f"Need: {purchase['need']}"
    )
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
        try:
            parsed = _parse_verdict(raw_answer, purchase["terms"] + "\n" + purchase["need"])
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
        "raw": raw,
    }
    
    with result_lock:
        result_list.append(result)


def judge_purchase(
    purchase: Dict[str, str],
    ask_fns: List[Callable[[List[Dict[str, str]]], str]],
    min_clear: int = int(os.getenv("SECONDLOOK_MIN_CLEAR", "3")),
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
    
    # Count clear verdicts
    clear_count = sum(1 for v in verdicts if v["verdict"] == "clear")
    total = len(verdicts)
    
    # Build summary
    hold_reasons = [v["reason"] for v in verdicts if v["verdict"] == "hold"]
    if hold_reasons:
        summary = f"{clear_count}/{total} clear; holds: {'; '.join(hold_reasons)}"
    else:
        summary = f"{clear_count}/{total} clear"
    
    return {
        "verdicts": verdicts,
        "clear_count": clear_count,
        "cleared": clear_count >= min_clear,
        "summary": summary,
    }