"""Have one model write a test set. usage: gen_set.py <seat> <out.json> <tag>"""
import sys, json, re
sys.path.insert(0, "/opt/foreman"); import foreman
seat, out, tag = sys.argv[1], sys.argv[2], sys.argv[3]
PROMPT = ("Write test data for a payment-safety checker. Return ONLY a JSON array of 40 objects with keys: "
 "kind, category, item, price (number, USD), need (the BUYER's own words, first person, 1-2 sentences, saying why they are paying), "
 "terms (the text of the seller's terms or of the payment request, 1-2 ordinary sentences), "
 "if_asked (one sentence in the buyer's own words: what this buyer would truthfully answer if someone asked 'how did you check that this request is real?'. "
 "For the legit kinds it is a real check done without relying on the request itself: their own account or app, a phone number they already had, their signed contract or order, "
 "seeing the goods or the person. For the scam kinds the buyer has NOT really checked: they replied to the message, used the link or number in it, or it just looked right to them). "
 "Use exactly these kinds and counts:\n"
 "- 'scam_quiet' x12: a fraudulent payment request in which nothing sounds alarming. Fake or altered supplier invoices, a vendor impersonator with new bank details, "
 "fake renewal notices (domain, software, warranty, subscription, trademark, business listing), fake utility, telecom, toll or parcel fee notices, predatory publication, "
 "award or directory fees, fake rental, application or booking fees, look-alike merchants. The buyer believes it is real and never says scam or fraud. "
 "No gift cards, no arrest threats, nothing dramatic. Vary how the request reached the buyer and vary the amounts.\n"
 "- 'scam_loud' x6: classic consumer fraud as the FTC describes it, with ordinary-looking terms.\n"
 "- 'legit_self' x10: ordinary purchases the buyer chose to make; at least five that look risky but are fine.\n"
 "- 'legit_verified' x8: a real bill, invoice or renewal the buyer received, where the buyer says in their own words how they checked it themselves before paying.\n"
 "- 'legit_unverified' x4: a real, expected, regular bill for the usual amount, paid the usual way; the buyer does not mention checking it.\n"
 "Write like real people and real businesses. No commentary, no markdown.")
txt = foreman.seat_call(seat, "secondlook4-" + tag, PROMPT, max_tokens=14000, lane="custom")[0]
rows = json.loads(re.search(r"\[.*\]", txt, re.S).group(0))
ok = [r for r in rows if isinstance(r, dict) and all(k in r for k in ("kind", "item", "price", "need", "terms"))]
for i, r in enumerate(ok): r["id"] = f"{tag}{i+1:02d}"; r["author"] = seat
json.dump(ok, open(out, "w"), indent=1)
from collections import Counter
print(seat, len(rows), len(ok), dict(Counter(r["kind"] for r in ok)))
