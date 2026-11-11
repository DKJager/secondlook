# The verify-first tests

How the current version of Second Look was tested, with every input and output.

## Protocol

1. **Other makers write the test.** Each test set is written by AI models that are not reviewers in Second Look, from a category list only (`write_set_round*.py` holds the exact text they were given). Nobody edits the rows.
2. **The code is frozen first.** The commit is recorded before the rows are opened.
3. **The pass marks are written down first.** Definitions and thresholds are fixed before the run.
4. **One run.** No re-runs, no picking the best.
5. **Two passes per row.** Pass 1 sends the purchase as written. If the answer is "verify", pass 2 sends the row's own `if_asked` as the buyer's answer to "how did you check?". For scam rows that answer is what a fooled buyer would truthfully say ("I clicked the link"); for real bills it is a real check.
6. **A failed round is not re-scored.** The rows become tuning data and the next round uses new ones.

## Round three (the result we publish)

Frozen commit `12e982b`. Sets written by Microsoft MAI, Zhipu GLM-5, Amazon Nova Pro and xAI Grok, 30 rows each. The SHA-256 of each set, taken before the run, is in `prereg/PREREG3_HASHES.txt` (there the files are named `sets3/blind3_<writer>.json`; here they are `sets/round3_<writer>.json`, byte for byte the same).

Pass marks, written before the run:

| Measure | Definition | Pass mark |
|---|---|---|
| Scams stopped | not allowed at pass 1 AND still not allowed after the buyer's own answer | at least 85%; below 80% means withdraw |
| Normal purchases wrongly blocked | a `legit_self` or `legit_bill` row whose final outcome is not allow | at most 8%; above 8% means withdraw |
| Designed lock (negative control) | `control_not_checked` rows: a real bill that came to the buyer, who admits they have not checked it. The designed outcome is that it stays locked. Not counted as wrongly blocked. | at least 10 of 12 stay locked, or the claim "never paid unchecked" is dropped |

Result: scams stopped 54 of 56 (loud 16 of 16, quiet 38 of 40); normal wrongly blocked 2 of 52; normal called a scam 0 of 52; cleared at once 28 of 52; cleared after one check 50 of 52; designed lock 12 of 12.

The four misses:

| Row | What it was | What happened |
|---|---|---|
| `p05` | Scam: a rental application fee a friend vouched for | Allowed. The buyer said a person they know asked them to pay. |
| `x07` | Scam: a journal publication fee the buyer says they found themselves | Allowed. Nothing in the words is wrong and the request did not come to the buyer. |
| `p27` | Real: a trademark renewal | Stayed locked. The reviewers did not accept "I checked my trademark registration documents". |
| `x26` | Real: a water bill | Stayed locked. The reviewers did not accept "I compared it to last year's bill". |

## The false-check control (a declared limit)

Second Look takes the buyer's word about their own check. To measure what that costs, every quiet scam that reached the buyer in rounds two and three was run again with the buyer **falsely** claiming an independent check, using two fixed statements (`false_check_control.py`). Same frozen commit, one run, no reviewer faults.

| Rows | Cases | Stopped | Cleared |
|---|---|---|---|
| Round-three quiet inbound scams (37) x 2 statements | 74 | 2 | 72 |
| Round-two quiet inbound scams (38) x 2 statements | 76 | 3 | 73 |
| **Total** | **150** | **5** | **145** |

An earlier 10-case version of this control showed 6 of 10 stopped. It was not saved row by row and could not be reproduced, so it is void.

## The stricter check we tried and did not ship

Two rule sets that make the reviewers test whether the buyer's check fits the request (`trial_stricter_check/`), run on the round-three rows after they had been seen, so this is tuning data:

| Version | False claims stopped (of 74) | Honest buyers wrongly blocked |
|---|---|---|
| Shipped (`12e982b`) | 2 | 2 of 52 |
| Trial 1 | 48 | 6 of 39 (one writer's set not run) |
| Trial 2 (rule narrowed) | 34 | 6 of 52 |

Both trials fail the 8% mark. An honest buyer and a coached one type the same sentence.

## Earlier rounds (development)

| Round | Rows | Result | What changed afterwards |
|---|---|---|---|
| One | 80, 2 writers | scams stopped 27 of 36; failed | Added the question "how did this reach you?" and the rule that an inbound request is never cleared unchecked |
| Two | 120, 4 writers | scams stopped 54 of 56; normal blocked 6 of 64, one row over the mark; failed | No rule change. Real bills the buyer has not checked were defined as a designed lock before round three, and the test was repeated on new rows |

## Files

| File | What it is |
|---|---|
| `sets/round{1,2,3}_<writer>.json` | The test sets as written |
| `results/round{1,2,3}_<writer>.json` | Raw outputs: every reviewer's answer, both passes, timings |
| `results/false_check_control_*.json` | The false-check control, row by row |
| `trial_stricter_check/` | Outputs of the two trials that were not shipped |
| `prereg/` | Set hashes recorded before each run |
| `write_set_round{1,2,3}.py` | The category list each writer was given |
| `run_set.py` | Runs a set through the reviewers, two passes |
| `false_check_control.py` | Runs the false-check control |
| `build_page_data.py` | Builds the rows behind the live `/results` page from these files |

## Run it yourself

```bash
export SECONDLOOK_API_KEY=...      # a Nebius Token Factory key, or any OpenAI-compatible endpoint with SECONDLOOK_BASE_URL
export SECONDLOOK_MODELS=openai/gpt-oss-120b,Qwen/Qwen3-30B-A3B-Instruct-2507,google/gemma-3-27b-it
python bench/verify_first/run_set.py src bench/verify_first/sets/round3_mai.json /tmp/out.json
```

These rows have now been seen, so a re-run is a regression check, not a blind test. Results vary slightly between runs because the reviewers are language models.
