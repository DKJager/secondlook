"""Build src/secondlook/test_results.json (the rows behind /results) from the run outputs in this folder.

    python3 bench/verify_first/build_page_data.py

Round three and the false-check control are read from results/. Earlier rounds are carried over unchanged
from the existing page data. No network, no key.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "..", "..", "src", "secondlook", "test_results.json")
WRITERS = {"mai-thinking": "Microsoft MAI", "glm-5-br": "Zhipu GLM-5", "nova-pro": "Amazon Nova Pro", "grok-42": "xAI Grok"}

SETS = [
    {"id": "round3", "name": "Current version", "sub": "120 purchases, 4 makers, 10 Nov",
     "about": "The version that is live. Written by Microsoft MAI, Zhipu GLM-5, Amazon Nova Pro and xAI Grok from a category list only, and run once "
              "after the code was frozen, against pass marks written down beforehand. A scam counts as stopped only if it was not allowed at first AND "
              "still not allowed after the buyer answered \"how did you check?\" truthfully. \"Normal, not checked\" rows are real bills where the buyer "
              "admits they have not checked: the right result is that they stay locked."},
    {"id": "lie", "lie": True, "name": "Declared limit: a buyer who lies", "sub": "150 cases, 10 Nov",
     "about": "Second Look takes the buyer's word about their own check. Here every quiet scam that reached the buyer in rounds two and three is run again "
              "with the buyer FALSELY claiming an independent check. Almost all are cleared. Second Look protects a buyer who is being fooled and answers "
              "honestly. It does not catch a buyer who has been coached to lie: the check box warns them, and the real fix is payee history on the "
              "payment provider's side."},
    {"id": "blind2", "name": "Round two (development)", "sub": "120 purchases, 4 makers, 10 Nov",
     "about": "The second blind round. It passed on scams and missed its pass mark for normal purchases by one row, so the test was repeated with new "
              "rows as round three instead of being re-scored. These rows were then used for tuning and are no longer a blind test."},
    {"id": "blind1", "name": "Round one (development)", "sub": "80 purchases, 2 makers, 10 Nov",
     "about": "The version before Second Look asked how the request reached the buyer. It missed 9 of 36 scams, because a fake renewal and a real one "
              "read exactly the same. That result is why the question was added. These rows were then used for tuning."},
    {"id": "first61", "name": "First version", "sub": "61 purchases, 1 maker, 9 Nov",
     "about": "The first version: clear or hold only. It missed 5 quiet fake invoices. These rows were later used for tuning, so they are no longer a blind test."},
]


def reasons(vs):
    return "; ".join(v["reason"] for v in vs if v.get("reason") and (v.get("v") or v.get("verdict")) != "clear")


def round3():
    out = []
    for a in ("mai", "glm", "nova", "grok"):
        for r in json.load(open(os.path.join(HERE, "results", f"round3_{a}.json")))["rows"]:
            kind = r["kind"]
            label = "scam" if kind.startswith("scam") else ("control" if kind == "control_not_checked" else "normal")
            final = r["final"]
            correct = final != "allow" if label != "normal" else final == "allow"
            out.append({"set": "round3", "id": r["id"], "writer": WRITERS.get(r["author"], r["author"]), "label": label, "kind": kind,
                        "item": r["item"], "price": r["price"], "origin": r.get("origin") or "", "need": r["need"],
                        "asked": r.get("if_asked") or "" if r["decision"] == "verify" else "",
                        "first": r["decision"], "final": final, "correct": correct,
                        "reasons": reasons(r.get("second") or r["verdicts"]) if final != "allow" or label != "normal" else ""})
    return out


def lies():
    out = []
    for f, tag in (("false_check_control_round3rows.json", "r3"), ("false_check_control_round2rows.json", "r2")):
        for i, r in enumerate(json.load(open(os.path.join(HERE, "results", f)))["rows"]):
            out.append({"set": "lie", "id": f"{tag}-{r['id']}-{i % 2 + 1}", "writer": WRITERS.get(r.get("author"), r.get("author") or ""), "label": "scam",
                        "kind": "false_check", "item": r["item"], "price": r["price"], "origin": "contacted", "need": r["need"],
                        "asked": r["false_statement"] + " (FALSE)", "first": "verify", "final": r["decision"], "correct": r["decision"] != "allow",
                        "reasons": reasons(r["verdicts"])})
    return out


def main():
    old = json.load(open(PAGE))
    old_rows = old["rows"] if isinstance(old, dict) else old
    keep = [r for r in old_rows if r["set"] in ("blind2", "blind1", "first61")]
    rows = round3() + lies() + keep
    json.dump({"sets": SETS, "rows": rows}, open(PAGE, "w"), ensure_ascii=False, separators=(",", ":"))
    for s in SETS:
        x = [r for r in rows if r["set"] == s["id"]]
        sc = [r for r in x if r["label"] == "scam"]; no = [r for r in x if r["label"] == "normal"]; ct = [r for r in x if r["label"] == "control"]
        print(s["id"], len(x), "| scams stopped", sum(r["correct"] for r in sc), "of", len(sc), "| normal blocked", sum(r["final"] != "allow" for r in no), "of", len(no),
              "| at once", sum(r["first"] == "allow" for r in no), "| called a scam", sum("hold" in (r["first"], r["final"]) for r in no), "| control locked", sum(r["correct"] for r in ct), "of", len(ct))


if __name__ == "__main__":
    main()
