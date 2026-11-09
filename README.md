# Second Look

**A pause before money moves.** Before PayPal creates an order, three AI reviewers from three makers check the purchase for scam signals. One objection stops the payment, and the objection quotes the exact words that caused it. PayPal's Orders API is called only with a clearance the server signed for that exact purchase.

## Try it in one minute

Open the live demo: https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws/

| Do this | See this |
|---|---|
| Click **Load scam example**, then **Ask the crowd** | Three HOLD verdicts, each quoting the buyer ("My grandson is in jail and needs bail tonight..."). The pay button stays locked. |
| Click **Load normal example**, then **Ask the crowd**, then **Confirm & Pay** | Three CLEAR verdicts, then a PayPal sandbox order ID and an approval link. |
| Run the forged-order command below | HTTP 409: `no valid crowd clearance for this exact purchase` |

```bash
curl -s -X POST https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws/order \
  -H 'content-type: application/json' \
  -d '{"purchase":{"item":"x","price":"5.00","currency":"USD","need":"n","terms":"t"},"verdict":{"cleared":true}}'
```

## Measured results (9 Nov 2026)

A different AI model (DeepSeek) wrote 61 purchases blind: 33 scams in the FTC's fraud categories with ordinary-looking seller terms, and 28 normal purchases, half of them chosen to look risky. We never tuned against this set.

| Blind test, 61 purchases | Before the 9 Nov update | Now |
|---|---|---|
| Scams stopped (of 33) | 30 | 28 |
| Normal purchases approved (of 28) | 18 | 25 |

- **The trade-off:** the update misses 5 scams where the earlier version missed 3, and approves 7 more normal purchases. The earlier version blocked 10 of 28 normal purchases, mostly because one model refused to "process a transaction".
- **What it misses:** quiet fake invoices where nothing in the text looks wrong (a toner invoice, a domain renewal notice, a utility program email, a journal fee, a rental application link).
- **Urgent but normal purchases:** 5 tested (a locksmith, a tow truck, a same-day flight to a hospital, an urgent-care copay, a wire to a contractor under contract); none blocked.
- **Our own 40 purchases** (the set we tuned on): the live demo stops 20 of 20 scams and approves 20 of 20 normal purchases; the median answer takes about 1 second.

Everything is in [`bench/`](bench/): the scenarios, the result files, and two scripts that re-run the tests against the live demo with no key:

```bash
python bench/run_blind_61_live.py
python bench/run_dev_40_live.py
```

Results vary slightly from run to run because the reviewers are language models.

![A scam stopped](docs/screenshot.png)

![A normal purchase cleared and a PayPal sandbox order created](docs/screenshot_order.png)

## How it works

1. The buyer, or the buyer's AI agent, sends the item, the price, why they are buying it, and the seller's terms.
2. Three open-weight models from three makers review it independently, in parallel.
3. Each returns `clear` or `hold`, a short reason that names the scam pattern, and a quote. The server keeps a quote only if it appears word for word in what the buyer or the seller wrote.
4. If any reviewer holds, the server issues no clearance and PayPal is never called.
5. If all three clear, the server issues a clearance token: an HMAC-SHA256 signature over the exact purchase fields, a 5-minute expiry and a random nonce.
6. `POST /order` creates a PayPal order only with a valid token for that exact purchase. The token's nonce is sent as `PayPal-Request-Id`, so a replayed token returns the same order and never a second one.
7. The buyer approves in PayPal, and the server captures the payment.

## For AI shopping agents

`POST /agent/check` is the same review as a single call for an agent to make before checkout:

```bash
curl -s -X POST https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws/agent/check \
  -H 'content-type: application/json' \
  -d '{"purchase":{"item":"Target gift cards x6","price":"900.00","currency":"USD","need":"My grandson is in jail and needs bail tonight","terms":"Gift cards are non-refundable."}}'
```

It returns `decision` (`allow` or `hold`), each reviewer's `verdict`, `reason`, `quote` and `quote_from`, and a `clearance_token` on allow. The agent then calls `POST /order` with `{purchase, clearance_token}`. The API is described at [`/openapi.json`](https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws/openapi.json).

## PayPal integration

The product uses PayPal Orders v2 sandbox API:

- `POST /v2/checkout/orders` with `intent` = `CAPTURE`
- `POST /v2/checkout/orders/{id}/capture`

All calls include the `PayPal-Request-Id` header and target the sandbox base URL `api-m.sandbox.paypal.com`. For order creation the header is the clearance token's nonce, which ties one clearance to one order. The order's `description` and `custom_id` record that Second Look cleared it.

## Models

The live deployment uses these three models via Nebius Token Factory:

- `openai/gpt-oss-120b`
- `Qwen/Qwen3-30B-A3B-Instruct-2507`
- `google/gemma-3-27b-it`

The code default in the `SECONDLOOK_MODELS` environment variable is:

- `nvidia/nemotron-3-super-120b-a12b`
- `Qwen/Qwen3-30B-A3B-Instruct-2507`
- `google/gemma-3-27b-it`

The live deployment overrides the first model to `openai/gpt-oss-120b` because Nemotron, a reasoning model, often returned empty content within the small answer budget. The three model makers are OpenAI (open-weight), Alibaba Qwen, and Google.

## Failure handling

The system handles reviewer failures without blocking the overall verdict:

- A reviewer that errors counts as a hold with reason "model error: {details}".
- A reviewer with no parseable JSON counts as a hold with reason "unparseable answer".
- A reviewer that returns nothing counts as a hold with reason "no answer from reviewer".
- A reviewer that refuses to review ("transaction disallowed") is asked once more; a refusal is not a reasoned hold.
- Quotes not found word for word in the buyer's reason or the seller's terms are dropped from the verdict.

## Verify in the sandbox console

After a cleared purchase, find the order by its ID in the PayPal sandbox console. The purchase unit's description reads "Second Look: cleared by 3 of 3 reviewers", and the custom ID starts with `secondlook:`.

## Run locally

Start the handler on localhost:8080:

```bash
python -m src.secondlook.handler
```

The server implements the same routes as the Lambda deployment: `GET /` serves the page, `POST /judge` and `POST /agent/check` run the reviewers, `POST /order` creates a sandbox PayPal order for a valid clearance, `POST /capture` captures an approved order, and `GET /openapi.json` describes the API.

Environment variables:

| Variable | Purpose |
|---|---|
| `SECONDLOOK_MODELS` | Comma-separated model IDs (default: three models above) |
| `SECONDLOOK_BASE_URL` | OpenAI-compatible endpoint (default: Nebius Token Factory) |
| `SECONDLOOK_API_KEY` | API key for the model endpoint |
| `SECONDLOOK_MIN_CLEAR` | Minimum clear votes required (default: 3) |
| `PAYPAL_CLIENT_ID` | PayPal sandbox client ID |
| `PAYPAL_SECRET` | PayPal sandbox secret |
| `SECONDLOOK_TOKEN_SEED` | Optional secret for signing clearance tokens (default: derived from the PayPal secret) |

## Tests

Run the 15 automated tests with a fake model and PayPal calls, no network or keys needed:

```bash
python -m pytest tests -q
```

## Deploy

Deploy with the script at `deploy/sl_deploy.py` (requires boto3). The script reads the model key from AWS Secrets Manager and PayPal sandbox credentials from SSM Parameter Store at deploy time. No credentials are stored in the repository.

## Privacy

The service stores nothing server-side. No purchase data, model outputs, or PayPal responses persist to disk or a database. All information lives only for the duration of the request and is returned to the caller.

## AI use

The product uses three external large language models listed in the Models section above. The application code was written with AI coding assistance and reviewed and tested by the owner.

## License

Apache-2.0
