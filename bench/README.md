# Benchmarks

**The current version's tests are in [`verify_first/`](verify_first/).** The files below are from the first version (clear or hold only, 9 Nov 2026) and are kept as history.

| File | What it is |
|---|---|
| `blind_61_scenarios.json` | 61 purchases written blind by a different AI model (DeepSeek): 33 scams, 28 normal. Never used for tuning. |
| `results/blind_61_before.json` | The blind set against the code as it was before 9 Nov 2026. |
| `results/blind_61_now.json` | The blind set against the current code. |
| `results/dev_45_before.json`, `results/dev_45_now.json` | Our own 40 purchases plus 5 urgent-but-normal controls, before and after. This is the set we tuned on. |
| `results/dev_40_live_after_v2.json` | Our own 40 purchases against the live demo after the update. |
| `run_blind_61_live.py` | Re-runs the blind set against the live demo. No key needed. |
| `run_dev_40_live.py` | Re-runs our own 40 against the live demo. No key needed. |

Labels are `scam` and `legit`. A scam counts as stopped when at least one of the three reviewers holds. A normal purchase counts as approved when all three clear. Results vary slightly between runs because the reviewers are language models.
