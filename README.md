# Rebuttal

**The dispute deadline agent for small PayPal sellers.** Rebuttal watches every PayPal dispute's
clock, investigates it against the seller's own records, works out whether to fight, offer or
refund, builds the evidence PayPal asks for, and sends it once the seller approves.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/) (Nov 2026).

**Live demo:** https://rebuttal-761390104675.us-central1.run.app. Demo data plus the live AI agent,
so no account is needed. Everyone shares one copy, and **Reset demo** puts it back.

**Live on the PayPal sandbox:** https://rebuttal-sandbox-761390104675.us-central1.run.app. This one
is connected to a real PayPal sandbox app and receives its dispute webhooks.

## The problem

A small seller gets a PayPal dispute. They have a few days to respond, they don't know which
evidence PayPal wants for that reason code, and the evidence is spread across their order
system, the carrier's site and old emails. So they miss the deadline, send a weak reply, or
fight a case they were always going to lose and pay the dispute fee on top.

The demo seller is **Ember & Clay**, a small ceramics studio in Jaipur that sells to buyers in
the US. Cross-border sellers like this lose the most: long shipping times invite "not received"
claims, and posting an item back to India costs more than many items are worth.

## What it does

1. **Watches.** It pulls disputes from the PayPal Disputes API (and PayPal dispute webhooks), and
   gives every open case a live countdown to `seller_response_due_date`. The disputes board is an
   [AG Grid](https://www.ag-grid.com/) (Community edition): sort by time left, amount or evidence
   strength, filter any column, search across buyers and items, with a pinned total of the money
   in dispute. A seller with forty open cases sees the next one to answer at the top.
2. **Investigates.** A tool-using AI agent (Gemini on Google Cloud Vertex AI) reads the dispute, the
   order, the carrier's event history, the emails with the buyer, the listing and the shop's policies.
   In "not as described" cases it also asks Gemini to compare the photo the buyer sent with the
   listing photo.
3. **Decides with numbers, not vibes.** Code, not the model, rates the evidence (strong, medium or
   weak) and compares the expected cost of each option: fight, partial refund (buyer keeps the item),
   refund after return, or refund in full. Rebuttal says "refund this one" when the records say
   the seller would lose.
4. **Drafts.** Notes for PayPal in plain language, a short message to the buyer, and evidence PDFs
   built only from the seller's records. Every PDF footer lists the records it came from.
5. **Checks before anything is sent.** These checks are deterministic, run against PayPal's latest
   copy of the dispute, and are shown to the seller:
   - the deadline is still open, and PayPal currently allows the action (its HATEOAS link is present
     and the offer type is in `allowed_response_options`);
   - every evidence type PayPal requested is included;
   - every tracking number matches the recorded shipment;
   - every fact cites a record that exists (`order:EC-2041`, `shipment:EC-2041`, `message:3` …);
   - notes fit PayPal's 2,000 characters, the offer amount is within the disputed amount, and the
     case is winnable.
6. **Shows where disputes cost money.** The **Insights** page is an [AG Studio](https://www.ag-grid.com/studio/)
   report over every dispute: money disputed, kept and lost, win rate, the **dispute rate** (disputes ÷
   orders, which PayPal watches), answers before the deadline, losses by reason, and whether following
   Rebuttal's advice paid off. The seller can edit it or build new charts. In demo mode it includes six
   months of generated history, labelled as such; in sandbox mode it shows only real cases.
7. **Acts on approval.** One click sends it: `provide-evidence` (multipart, with PDFs), `make-offer`,
   `accept-claim`, `send-message`, or `provide-supporting-info` when PayPal is already reviewing a
   claim. An optional **autopilot** answers small disputes without a click, but only when every check
   passes and the plan matches the cost comparison.

## Try it in two minutes (no PayPal account needed)

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Aditya-galaxy/rebuttal && cd rebuttal
uv run uvicorn rebuttal.web:app --port 8777
```

Open http://localhost:8777. This is **demo mode**: an in-memory PayPal with the same API shapes and
rules as the real one, plus four Ember & Clay disputes that each get a different answer:

| Dispute | What the records show | Rebuttal recommends |
|---|---|---|
| Vase "not received", $148 | DHL delivered it, signed for by the buyer | Fight with evidence |
| Cups "not as described", $62 | The listing discloses that hand-fired glaze colour varies | Offer 30% back, buyer keeps them |
| Coasters "not received", $35 | India Post tracking stopped 25 days ago | Refund in full: fighting would lose |
| "Unauthorised payment", $210 | Signed delivery, and the buyer emailed asking for gift wrap | Fight with evidence |

Open a case, read the investigation and the checks, click **Approve**, then use the sandbox controls
to have PayPal rule on it. In **Storefront**, you can buy something, choose what happens to the
parcel, and open a dispute as the buyer.

Without a model, plans are written from templates (the case page says so). To see the agent, either
use Gemini on Vertex AI with your Google Cloud project:

```bash
gcloud auth application-default login
VERTEX_PROJECT=<your-project> uv run uvicorn rebuttal.web:app --port 8777
```

or any OpenAI-compatible API with tool calls (we also ran it on DeepSeek V4 Pro on Nebius Token Factory):

```bash
LLM_PROVIDER=nebius LLM_API_KEY=... uv run uvicorn rebuttal.web:app --port 8777
```

## Run it against the PayPal sandbox

1. In the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/) (Sandbox), create
   **US** business and personal sandbox accounts. A merchant app on the US business account gives
   you a client id and secret. (Sandbox checkout fails if buyer and seller are both Indian accounts.)
2. `cp .env.example .env`, set `PAYPAL_MODE=sandbox` and the two credentials, then check:
   ```bash
   uv run rebuttal check
   ```
3. Start the app and open **Storefront**. Pay with the sandbox *personal* account using the PayPal
   button. Rebuttal records the order and uploads a FedEx tracker to PayPal.
4. Log in to https://www.sandbox.paypal.com as that buyer and report a problem with the payment.
5. Click **Check PayPal now**. Sandbox disputes take about 3 minutes to reach the API.

### What we verified live in the sandbox (3–7 Nov 2026)

- Order `EC-2071` was paid through PayPal Checkout (Orders v2 capture, transaction `1FK2168823766710E`),
  and its tracker was uploaded as `DELIVERED`.
- The buyer filed "I didn't receive an item". The sandbox opened dispute **PP-R-TLZ-10196141**
  straight into PayPal review, where the only seller action is `provide_supporting_info`.
- Rebuttal picked it up, the agent drafted a cited statement, and all checks passed. After approval,
  PayPal's dispute record shows the statement and `proof-of-delivery.pdf` under
  `supporting_info`, `source: SUBMITTED_BY_SELLER`.
- The hosted sandbox service receives `CUSTOMER.DISPUTE.*` webhooks and checks each one with PayPal's
  `verify-webhook-signature`. PayPal's own simulated event passed the check. An unsigned POST
  is refused with 400.

- Order `EC-2073` shipped on the real clock (FedEx from Jaipur, about four days to a signed delivery);
  PayPal's tracker moved from Shipped to Delivered when it arrived on 7 Nov.
- About 80 minutes later the buyer filed "not received" as **PP-R-ZBY-10196965**. It reached Rebuttal
  by webhook, with no click. On the first run the gate blocked Gemini's draft for citing a record
  that doesn't exist; after the fix (and a test), all nine checks passed. The seller approved, and
  PayPal's record shows `proof-of-delivery.pdf` under `supporting_info`, `source: SUBMITTED_BY_SELLER`.
  This is the case in the demo video.
- On the hosted demo, Gemini 2.5 Flash on Vertex AI drafts all four demo cases with every check
  passing, and the photo check runs on the chai-cup case.

That run is also why Rebuttal handles claims already under review. Real dispute objects can return
`null` for `messages`, `allowed_response_options` and the due date, and the code is written for that.

## How the AI is used

| Part | Done by |
|---|---|
| Reading the dispute and the records, choosing what matters | Agent (Gemini, tool calls) |
| Comparing the buyer's photo with the listing photo | Gemini vision, shown as an AI observation |
| Writing the statement to PayPal and the buyer message | Agent |
| Evidence strength, cost of each option, recommended action | Code (`playbook.py`) |
| Whether anything may be sent | Code (`gate.py`) |
| Sending | The seller's click, or autopilot under the seller's limit |

- **Model.** Gemini 2.5 Flash on **Google Cloud Vertex AI** (`VERTEX_PROJECT`), through Vertex's
  OpenAI-compatible endpoint: about 10–20 s per case. Without Vertex, any OpenAI-compatible chat API
  with tool calls works (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`); we also ran it on DeepSeek V4 Pro
  on Nebius Token Factory.
- **Photo evidence.** In "not as described" cases, Gemini compares the photo the buyer sent with the
  listing photo and reports what visibly differs, whether the listing's own disclosure covers it, and
  any damage. The case page shows it, the agent can cite it, and a photo-comparison PDF puts both
  photos side by side, labelled as an AI observation.
- **Images.** Ember & Clay is fictional, so its product photos, studio banner and the buyer's photo
  were generated with Gemini 2.5 Flash Image on Vertex AI (`tools/make_images.py`). The buyer's photo
  was made from the listing photo, so it shows the same cups as a customer would photograph them.
- **Tools.** `get_dispute`, `get_assessment`, `find_order`, `get_shipment`, `get_messages`,
  `get_product`, `get_policy`, `check_photos`, `submit_plan`. The case page shows each step live while it runs, and
  the full trace afterwards.
- **Guardrails.** Buyer-written text reaches the model labelled as untrusted data. The model can't
  invent facts that get past the gate. If the model fails, Rebuttal falls back to a template plan and
  says so on screen.

## PayPal developer platform used

| API | Used for |
|---|---|
| Disputes v1: list, show | Watching disputes, deadlines, requested evidence, allowed actions |
| Disputes v1: `provide-evidence` (multipart + PDFs) | Fighting a dispute |
| Disputes v1: `provide-supporting-info` | Adding to a claim PayPal is already reviewing |
| Disputes v1: `make-offer`, `accept-claim`, `send-message` | Offers, refunds, buyer messages |
| Disputes v1: `require-evidence`, `adjudicate` (sandbox only) | Demo controls |
| Orders v2 + JavaScript SDK buttons | The demo storefront's checkout |
| Shipment tracking (`/v1/shipping/trackers-batch`) | Uploading the carrier tracker |
| Webhooks: `verify-webhook-signature`, `CUSTOMER.DISPUTE.*` | New disputes without polling |

## Code map

```
rebuttal/
  paypal.py         PayPal REST client (OAuth, disputes, orders, tracking, webhooks)
  fake_paypal.py    In-memory PayPal with the same methods and rules, for demo mode and tests
  shop.py           The seller's records (SQLite) and the Ember & Clay demo data
  playbook.py       Evidence per reason code, evidence strength, cost of each option
  agent.py          The tool-using agent and its fallback
  plan.py           Plans, evidence PDFs, and turning an approved plan into PayPal calls
  evidence_docs.py  PDF documents built from records
  gate.py           The checks before sending
  engine.py         Sync, investigate, approve, send
  web.py            FastAPI app: disputes board, case review, settings, storefront, webhook
  static/board.js   The disputes board as an AG Grid, with live countdown cells
  insights.py       The data behind Insights: every dispute's outcome, and orders and disputes by month
  vertex.py         Google Cloud Vertex AI: Gemini (agent, photo checks) and image generation
  photos.py         The Gemini photo check: the buyer's photo against the listing photo
  responses_bridge.py  Responses API requests served by a Chat Completions model (experimental)
tools/make_images.py   Generates the demo shop's images with Gemini on Vertex AI
frontend/           The Insights page script (AG Studio report and data model), built to static/insights.js
  persist.py        Keeps the SQLite files on a mounted folder across restarts
  cli.py            `rebuttal check`
tests/test_core.py  40 tests: decisions, every gate rule, PayPal rules, the agent loop and its fallbacks,
                    webhooks, Insights data, the AI proxy, carrier timing, concurrency
```

```bash
uv run pytest
```

## Building the Insights page

AG Studio loads from the CDN; only Rebuttal's page script is built, and the built file is committed.

```bash
cd frontend && npm install && npm run build   # writes ../rebuttal/static/insights.js
```

AG Studio is a commercial product. Without a licence key it runs fully unlocked with a trial watermark;
set `AG_STUDIO_LICENSE_KEY` to remove it. AG Studio's built-in AI assistant is wired up through a proxy
(`/api/ai/responses`), but it is off by default. Its data agent's query tool has a recursive schema
built for OpenAI's strict tool calling. With Nebius models its data agent stalled; Vertex AI's Gemini
endpoints reject the schema, and through a simplified one (`responses_bridge.py`) its queries fail.
Set `INSIGHTS_AI=1` with `INSIGHTS_LLM_API_KEY` (OpenAI) to turn it on, or `INSIGHTS_AI=vertex` to try
Gemini.

## Running it as a container

```bash
docker build -t rebuttal .
docker run --rm -p 8080:8080 rebuttal                    # demo mode
docker run --rm -p 8080:8080 --env-file .env rebuttal    # your settings, e.g. the sandbox
```

Hosts without a lasting disk can set `SNAPSHOT_DIR` to a mounted folder (a storage bucket, for
example). Rebuttal restores its two SQLite files from there on start and copies them back whenever
they change.

## Honest limits

- **Simulated records.** The shop's records and carrier events are demo data. A real seller would
  connect their store (Shopify, WooCommerce) and carrier accounts. The PayPal side is real in sandbox
  mode.
- **Estimated costs.** Win odds per strength level are fixed estimates, and the dispute fee and
  return shipping cost come from Settings.
- **Sandbox outcomes.** The sandbox only lets `adjudicate` run in certain states. A claim that opens
  directly into review stays "in review".
- **Generated images.** Ember & Clay is fictional; its product photos, banner and the buyer's photo
  are AI-generated (see `tools/make_images.py`).
- **Photo checks are observations.** Gemini's photo comparison informs the draft and is labelled as AI;
  it never changes the computed evidence strength.
- **Insights assistant off.** AG Studio's built-in AI assistant is off in the hosted demo (see
  "Building the Insights page" for why).

## License

[Apache-2.0](LICENSE)
