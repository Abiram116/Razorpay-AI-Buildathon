# Architecture — the full technical writeup

This is the deep-dive: every phase, every design decision, every guarantee
I built into the code and how I verified it. The main [README](README.md)
is the short version for anyone deciding whether to dig in here.

---

# AI Chargeback Defense Agent

**Razorpay AI Buildathon — Track 2: AI Risk Manager**

> Razorpay tells me a payment has become a dispute. My AI investigates
> whether the merchant actually has a defensible case, gathers the evidence,
> explains the reasoning, and prepares the contest — while keeping the final
> decision and submission firmly in a human's hands.

**Status: Phase 7 of 10 complete** (Razorpay contest integration). See "Build phases".

---

## 1. The problem

When a customer or issuing bank disputes a payment, the merchant has a short,
hard deadline (`respond_by`) to contest it with evidence. Today that means a
human digging through order systems, courier tracking, support inboxes and
refund records for every single case — expensive, slow, and easy to get wrong
in both directions: contesting cases that cannot be won, and silently losing
cases that could have been.

## 2. What this is

This is **not** a fraud detector and **not** a replacement for Razorpay's risk
systems. It starts *after* a dispute exists:

```
Razorpay payment → dispute raised → INGEST → gather evidence → AI investigates
   → STRONG / WEAK / NO CASE → evidence package → HUMAN REVIEW
   → approve → contest draft → HUMAN SUBMITS → Razorpay → issuing bank
```

The AI **never** submits a contest. A human must approve first.

## 3. Data provenance — read this first

The project keeps three data sources strictly separated and always labelled:

| Class | What it is | Where |
|---|---|---|
| **A. Real Razorpay Test Mode data** | Genuine orders/payments/disputes from `api.razorpay.com` using `rzp_test_` keys | `src/razorpay_client.py` |
| **B. Synthetic merchant-side data** | My own SQLite store standing in for the merchant's internal systems (shipments, support threads, refunds, policies) | `data/merchant/` |
| **C. Synthetic evaluation data** | Labelled dispute cases with hidden ground truth, used only to measure the AI | `data/evaluation/` |

Synthetic data is **never** presented as real Razorpay production data. Any
simulated dispute is labelled *Simulated Test Dispute* in the UI and stored
with an explicit `is_simulated` flag.

## 4. Razorpay integration

Verified against the official API and SDK (`razorpay` 2.0.1):

| Operation | Endpoint | Used for |
|---|---|---|
| Auth probe | `GET /v1/payments?count=1` | Read-only credential check |
| Create order | `POST /v1/orders` | Real test-mode transaction |
| Fetch order/payment | `GET /v1/orders/{id}`, `/v1/payments/{id}` | Case context |
| Fetch disputes | `GET /v1/disputes`, `/v1/disputes/{id}` | Dispute facts |
| Upload evidence | `POST /v1/documents` (`purpose=dispute_evidence`) | PDF/PNG/JPG, ≤50 MB |
| Contest | `PATCH /v1/disputes/{id}/contest` | `action: "draft"` then `"submit"` |
| Webhook auth | HMAC-SHA256 over the **raw** body vs `X-Razorpay-Signature` | `payment.dispute.created` |

**Important documented constraints** the code enforces rather than assumes:
- The evidence `summary` field has a **1000 character maximum**.
- A `submit` needs **at least one document id**.
- **There is no "create dispute" API** — disputes are raised by the issuing
  bank or the customer, in Live *and* Test Mode. I checked this properly
  before building around it — see `NOTES.md` N-003 for how I dealt with it.

## 5. Safety rails already in place

- Live keys (`rzp_live_`) are **always refused** — this prototype only runs in Test Mode.
- Secrets are never printed; `config.redact()` is the only way key material
  reaches a log or the UI.
- Razorpay failures become typed errors (`RazorpayAuthError`,
  `RazorpayUnavailable`, `RazorpayRequestError`) that route a case to manual
  review instead of crashing or inventing data.
- Only whitelisted, non-PII entity fields are ever logged.

## 6. Setup

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env      # paste Razorpay TEST keys + Groq key
```

Open the folder in VS Code and any new integrated terminal starts at the
project root with `.venv` already active (`.vscode/settings.json`). Outside
VS Code, `source .venv/bin/activate`.

`.env` holds four values, and nothing else — every other setting is a
non-secret tunable in the `TUNABLES` block at the top of `src/config.py`:

| Variable | Where to get it |
|---|---|
| `RAZORPAY_KEY_ID` | Dashboard → **Test Mode** → Settings → API Keys (must start `rzp_test_`) |
| `RAZORPAY_KEY_SECRET` | shown once when you generate the key |
| `RAZORPAY_WEBHOOK_SECRET` | the secret you set on your webhook endpoint |
| `GROQ_API_KEY` | <https://console.groq.com/keys> |

`.env` is git-ignored (`.gitignore:2`).

Verify the integration:

```bash
python scripts/verify_phase1.py     # venv active
# or, without activating:
uv run scripts/verify_phase1.py
```

## 7. Build phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Razorpay integration + credential verification | **complete** |
| 2 | Dispute ingestion — webhook + labelled simulator | **complete** |
| 3 | Merchant SQLite database + seed data | **complete** |
| 4 | AI investigation agent — Groq, strict JSON, validated | **complete** |
| 5 | Evidence builder + document generation | **complete** |
| 6 | Streamlit human-review dashboard | **complete** |
| 7 | Contest draft → human submit | **complete** |
| 8 | Held-out evaluation: precision/recall/F1 + financial impact | next |
| 9 | Failure-mode demonstration | pending |
| 10 | Polish | pending |

## 7b. Dispute ingestion (Phase 2)

Two ways a dispute enters the system, one shared entrypoint downstream:

```
Razorpay ──webhook──▶ FastAPI (src/webhook_handler.py) ──┐
  payment.dispute.*    verify HMAC-SHA256 over raw body  │
  (real, but cannot                                       ├──▶ ingest_dispute()
   be triggered in                                        │    (src/ingestion.py)
   Test Mode — see §9)                                    │        │
                                                            │        ▼
Local simulator ──────────────────────────────────────────┘   cases.db
  (src/dispute_simulator.py)                                   state=INGESTED
  sim_disp_/sim_pay_/sim_order_ ids only,                       audit_log entry
  is_simulated=True forever
```

- **Signature verification**: raw request body is read *before* JSON parsing;
  HMAC-SHA256 over those exact bytes vs `X-Razorpay-Signature`, using the
  same algorithm as the official SDK (`razorpay.Utility.verify_webhook_signature`).
  An invalid or tampered body is rejected with 400 before any parsing happens.
- **Idempotency**: Razorpay's webhook envelope carries no delivery/event id
  (verified against the documented sample payload), so dedup keys on a
  SHA-256 hash of the raw body. A repeated delivery is acknowledged as
  `duplicate_ignored` and never reprocessed. See `BUILD_LOG.md` 2026-09-02-04.
- **Schema validation**: every field is checked against Razorpay's documented
  enums (`status ∈ {open, under_review, won, lost, closed}`,
  `phase ∈ {fraud, retrieval, chargeback, pre_arbitration, arbitration}`). An
  out-of-spec value is rejected, never guessed or coerced.
- **Real vs. simulated, enforced at the id level, not just a flag**: a real
  Razorpay id is `prefix_` + exactly 14 alphanumeric characters
  (`disp_AHfqOvkldwsbqt`, verified live in Phase 1). A simulated id is always
  `sim_prefix_...` and is structurally rejected by the real-data parser — a
  simulated case cannot be laundered into looking like a genuine one anywhere
  in the code path.
- **State machine**: every case starts at `INGESTED`; `src/database.py`
  enforces the full transition graph from §14 of the spec and writes an
  `audit_log` row on every change, from Phase 2 onward (later phases just add
  more transitions, not a new mechanism).

Try it:
```bash
uv run uvicorn src.webhook_handler:app --reload --port 8000   # terminal 1
uv run scripts/send_test_webhook.py                            # terminal 2
uv run scripts/send_test_webhook.py --bad-signature             # prove rejection
uv run scripts/run_simulator.py strong                          # no server needed
uv run scripts/run_simulator.py no-case
```

## 7c. Merchant database (Phase 3)

A second, independent SQLite database - `data/merchant/merchant.db` - stands
in for the merchant's own internal systems: order fulfilment, courier
tracking, support conversations, refunds, store policy, and evidence
documents. Razorpay has none of this; it only knows about the payment. This
is the counterpart the AI investigator (Phase 4) will actually reason over.

```
orders ──┬──▶ shipments               (0 or 1 - absent is normal for
         │                              product_type='digital'/'service')
         ├──▶ customer_communications  (0..n, chronological)
         ├──▶ refunds                 (0 or 1)
         └──▶ documents                (0..n, mapped onto Razorpay's own
                                         evidence categories - see below)

policies  (store-wide: refund/cancellation/T&Cs, versioned, latest wins)
```

- **Real vs. simulated, enforced the same way as Phase 2**: every
  `razorpay_order_id`/`payment_id` stored in `orders` is validated against
  the identical `REAL_ID_PATTERN` / `SIMULATED_ID_PATTERN` from
  `dispute_schema.py` - a merchant record can point at a genuine Razorpay
  test id or a labelled `sim_` one, never an ad-hoc string. `generate_simulated_id()`
  (the one place a `sim_` id is minted) is shared between the Phase 2
  simulator and Phase 3 seed data, not duplicated.
- **`document_type` mirrors Razorpay's own dispute evidence categories**
  exactly (`shipping_proof`, `customer_communication`,
  `access_activity_log`, `refund_confirmation`, ... - the same set fetched
  from the Contest a Dispute API in Phase 1), so Phase 5's evidence builder
  can map a merchant document straight onto a Razorpay evidence field with
  no second translation table.
- **One aggregation entrypoint for later phases**:
  `get_case_evidence(db, payment_id=...)` returns everything the merchant's
  systems can offer about one order - order, shipment-or-None, all
  communications, refund-or-None, documents, current policies - in a single
  call. Absence (e.g. no shipment for a digital order) is always `None`, not
  a fabricated empty record; a payment I have no record of at all returns
  `None` for the whole bundle, not a synthetic empty one, so "no evidence" and
  "wrong lookup" can never be confused with "the merchant confirmed nothing
  happened."
- **Seed data spans both defensible and indefensible cases on purpose** -
  `src/merchant_seed_data.py` ships 9 scenarios (`ORD-1001`..`ORD-1009`)
  including the mega-spec's two flagship worked examples (signed delivery +
  admitted receipt → STRONG_CASE; never shipped + ignored complaints →
  NO_CASE), a deliberately ambiguous case shaped like the one used to
  benchmark the Groq model in Phase 1, a digital product proved via access
  logs instead of shipping, a subscription the merchant's own records
  show was charged after cancellation, and two scenarios grounded in
  dispute patterns specific to Indian merchants: a UPI payment the
  customer's banking app showed as failed but which actually captured
  (`ORD-1008` — the single most common dispute pattern on Indian payment
  rails), and an RTO (return-to-origin) case with real courier NDR
  (non-delivery report) attempts logged, ultimately undelivered and
  unrefunded (`ORD-1009`). An `expected_strength` label travels with each
  scenario for my own dev sanity-checking only - it's Python-only,
  never written to the database, and must never reach the AI investigator.
- **Every case is investigated automatically the moment it's seeded** -
  a reviewer's job is to decide, never to remember to click "investigate"
  on each case first. The dashboard also auto-investigates on open (with a
  spinner) for any case that somehow reaches it without a saved result, and
  a sidebar bulk action investigates everything still pending in one click.

Seed it (also ingests a matching Simulated Test Dispute into `cases.db` and
runs the AI investigation for each scenario, by default):
```bash
uv run scripts/seed_merchant_db.py --reset
uv run scripts/seed_merchant_db.py --no-investigate  # skip the AI step
uv run scripts/seed_merchant_db.py --merchant-only   # skip case ingestion
```

## 7d. AI investigation agent (Phase 4)

```
dispute_id → load case (cases.db) → get_case_evidence() (merchant.db)
   → deterministic prompt → Groq (strict json_schema, temperature 0)
   → validate + verify every citation → InvestigationResult | InvestigationFailure
   → persisted to investigations table
```

**Code does the deterministic work; the model only does judgment.** Loading,
evidence assembly, date/amount formatting, validation, citation checking,
persistence and state are all ordinary Python. The model reads the narrative,
weighs conflicts, and explains its conclusion — nothing else.

- **Citations are verified, not trusted.** The prompt gives the model an
  explicit list of citable references (`document:7`, `communication:12`,
  `shipment:ORD-1001`). Every reference it returns must resolve against that
  whitelist, or the response is rejected and retried. This turns "don't
  invent evidence" from a prompt request into a code-enforced guarantee. A
  `STRONG_CASE` with no citations is rejected too.
- **Failure is a distinct type, never a quiet "no case".**
  `InvestigationFailure` (`CASE_NOT_FOUND`, `NO_MERCHANT_EVIDENCE`,
  `AI_UNAVAILABLE`, `INVALID_AI_RESPONSE`) can never be mistaken for a
  finding that the merchant has no case. Failures are persisted too, so an
  audit shows a case *was* attempted and failed.
- **Transient vs. permanent API errors are distinguished.** Rate limits,
  timeouts and 5xx back off and retry (using the delay Groq itself supplies);
  auth/bad-request errors fail fast. See `BUILD_LOG.md` 2026-09-02-08.
- **A bad response is retried once with the validation error fed back**, then
  fails safe to human review.
- **The dev-only `expected_strength` label never reaches the model** — there
  is a test asserting the prompt contains none of the classification strings.

Run it (never submits or contests anything — recommendation only):
```bash
uv run scripts/run_investigation.py            # all seeded cases
uv run scripts/run_investigation.py --verbose  # with full reasoning
```

## 7e. Evidence builder & documents (Phase 5)

```
InvestigationResult + CaseEvidence
   → select evidence categories (only those with real records behind them)
   → contest summary: compose → validate → AI-shorten → validate → truncate
   → explanation letter (from validated investigation text)
   → render every merchant record as an uploadable PDF
   → EvidencePackage (a DRAFT - nothing uploaded, nothing contested)
```

- **Categories are populated from records, never padded.** Razorpay exposes
  eleven; a category appears only when a concrete merchant record backs it,
  and each entry lists the references behind it. A `never_shipped` shipment
  does *not* become `shipping_proof` — offering it would damage the case, so
  it becomes a warning. A refund counts as `refund_confirmation` only when
  actually `processed`.
- **Digital vs physical is respected.** A digital order with no shipment gets
  `access_activity_log`/`proof_of_service` and no warning; a physical one with
  no shipment gets flagged as an evidence gap.
- **NO_CASE refuses to build by default.** Producing a polished, submittable-
  looking package for an unwinnable case is how rubber-stamping happens. A
  reviewer can override (`--force`), and the override is recorded in the
  package's warnings.
- **The 1000-char summary limit is enforced, never assumed.** Composed from
  already-validated investigation text (so no new hallucination surface),
  then shortened by the model only if oversized, re-validated, and truncated
  programmatically as a last resort — with a `SummaryTrace` recording which
  path it took.
- **Razorpay accepts only PDF/PNG/JPG**, so every merchant text record is
  rendered to PDF. A missing source file is stated in the PDF, never implied
  to have been read. See `BUILD_LOG.md` 2026-09-02-09 and -10.

```bash
uv run scripts/build_evidence.py                    # all investigated cases
uv run scripts/build_evidence.py <dispute_id> --force  # override a NO_CASE
```

## 7f. Human review dashboard (Phase 6)

```bash
uv run streamlit run dashboard/app.py     # http://localhost:8501
```

```
Overview  →  Dispute queue  →  Case facts  →  🤖 AI investigation
                                                     ↓
                                          (recommendation only)
                                                     ↓
                                              👤 HUMAN REVIEW
                                          ┌──────────┼──────────┐
                                      APPROVE     REJECT   REQUEST MORE
                                          ↓
                                    CONTEST DRAFT  (never auto-submitted)
```

- **The AI/human boundary is the visual centrepiece.** The verdict renders in
  a bordered card headed *"AI INVESTIGATION — RECOMMENDATION ONLY"*, followed
  by a separate *"Human review — the decision is yours"* section. The suggested
  action is shown as `not executed — a human decides`.
- **Every citation resolves to its record.** Each supporting-evidence entry is
  an expander showing the actual underlying row — the customer's message text,
  the courier's fields, the policy body. Nothing is displayed as fact without
  its source; an unresolvable citation renders as a warning.
- **Provenance is unmissable.** A `SIMULATED TEST DISPUTE` badge sits on every
  synthetic case, with a caption explaining that Razorpay provides no API to
  create test disputes. Each evidence tab is captioned with its source
  ("merchant's own order system (synthetic)" vs "Razorpay dispute record").
- **NO_CASE is a wall, not a speed bump.** Package generation is refused with
  the reasoning shown; the override button stays *disabled* until the reviewer
  ticks a confirmation box, and an active override is banner-flagged.
- **Deadlines drive urgency** using the Phase 1 config thresholds — EXPIRED
  cases show a blocking banner.
- **No submission path exists.** There is a test asserting the dashboard source
  contains no call to `contest_dispute` or `upload_evidence_document`.
- **A persistent workflow stepper** on every case page answers "where is this
  case right now" without re-reading the audit log — seven steps
  (Ingested → Investigating → Investigated → Awaiting Review → Approved →
  Drafted → Submitted), with a rejected case shown as reached-then-stopped
  rather than silently vanishing off the strip.
- **Raw enum values never reach the reviewer unexplained** — `reason_code`
  and `case_state` render as plain language (`review_workflow.reason_code_label`
  / `case_state_label`), with a sidebar glossary as one reference point for
  a queue that mixes many reasons and states in one sitting.

Demo data loads from the sidebar (**Load / reset demo cases**), labelled as
synthetic throughout.

## 7g. Contest integration (Phase 7)

```
APPROVED ──human──▶ save draft (action="draft") ──▶ DRAFTED
                                                      │
                                              human confirms
                                                      ▼
                          ┌───────────────────────────────────┐
                          │ Is this a REAL Razorpay dispute?  │
                          └──────────┬─────────────┬──────────┘
                                 YES │             │ NO (simulated)
                                     ▼             ▼
                        submit (action="submit")  BLOCKED in backend
                                     ▼
                                 SUBMITTED
```

Three operations, deliberately separate (`src/contest_service.py`):

| Function | Network | Guard |
|---|---|---|
| `build_local_draft()` | none | always safe — what a human inspects |
| `save_draft_to_razorpay()` | `action="draft"` | real disputes only |
| `submit_contest()` | `action="submit"` | real only **and** `human_confirmed=True` **and** a named actor |

- **Simulated disputes are blocked in the backend**, not the UI.
  `assert_submittable()` runs at the top of the shared send path, so the API
  is never reached for a `sim_disp_...` case — verified by a test asserting
  the mocked client records zero calls.
- **No default permits submission.** `human_confirmed` must be passed
  explicitly; a test asserts every dashboard call site does so behind a
  confirmation checkbox. Nothing goes from a STRONG_CASE verdict to a
  submission on its own.
- **State advances only after Razorpay confirms.** A failed call leaves the
  case at `APPROVED`, retryable, with the failure recorded in
  `contest_attempts`.
- **Uploads are idempotent** — `UNIQUE(dispute_id, local_path)` means a retry
  reuses existing `doc_...` ids instead of duplicating documents at Razorpay.
- **The 1000-char limit is re-checked here too**, so an oversized summary
  cannot reach Razorpay by any route.

## 8. Layout

```
.vscode/                 terminal opens at root with .venv active
src/config.py             secrets from .env, tunables in code, safety rails
src/razorpay_client.py    defensive wrapper over the Razorpay SDK
src/dispute_schema.py     the dispute/payment entity contract; real vs sim id rules
src/webhook_handler.py    FastAPI receiver: HMAC verify, parse, idempotency
src/dispute_simulator.py  labelled "Simulated Test Dispute" generator
src/ingestion.py          the one entrypoint both webhook and simulator call
src/database.py           case state machine + audit log + webhook idempotency
src/merchant_db.py         merchant's own data: orders, shipments, comms, refunds, docs, policies
src/merchant_seed_data.py  9 synthetic chargeback scenarios, both defensible and not
src/investigation_schema.py  result contract + citation verification (anti-hallucination)
src/investigation_agent.py   prompt building, Groq call, retry/backoff, failure states
src/evidence_builder.py      category mapping, contest summary limit pipeline
src/document_generator.py    evidence PDFs, explanation letter, case report
src/review_workflow.py       deadline urgency, queue stats, human decisions + audit
src/contest_service.py       draft/submit, upload idempotency, submission guards
dashboard/app.py             Streamlit review UI (a view over src/, no logic)
scripts/verify_phase1.py  end-to-end Phase 1 integration check
scripts/send_test_webhook.py  self-signed local webhook, proves the HMAC pipe
scripts/run_simulator.py  seed a Simulated Test Dispute without a server
scripts/seed_merchant_db.py  seed merchant.db (+ matching cases in cases.db)
scripts/run_investigation.py  run the AI investigator over seeded cases
scripts/build_evidence.py     build evidence packages + PDFs from investigations
docs/                    vendored official Razorpay chargeback reason codes
NOTES.md                 engineering log: problems, causes, decisions
```

## 9. Known limitations (current)

- Nobody — not me, not any merchant — can create a Razorpay-side dispute.
  I confirmed that properly instead of assuming it, so the end-to-end flow
  is demonstrated with a simulator built to match Razorpay's own dispute
  schema exactly (`NOTES.md` N-003).
- The cost figures in the `TUNABLES` block of `src/config.py` are **my own
  assumptions**, not published Razorpay fees. I left `CHARGEBACK_PENALTY_FEE_INR`
  as `None` on purpose rather than making up a number.
