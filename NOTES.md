# Engineering Notes

A running log of problems hit, what was tried, root causes, and decisions.
Newest entries at the bottom of each phase.

---

## Phase 1 — Razorpay Integration

### N-001 — `pip install` blocked by PEP 668
**Problem.** `pip3 install pypdf` failed with *externally-managed-environment*.
**Root cause.** Debian/Ubuntu ships Python 3.12 with PEP 668 marking the system
interpreter as externally managed; installing into it is refused by design.
**Solution.** Project virtualenv at `.venv/`, built with **uv** (`uv venv
--python 3.12`, `uv pip install -r requirements.txt`). No
`--break-system-packages` anywhere. `.vscode/settings.json` activates the venv
in every integrated terminal so contributors do not run against system Python.
**Gotcha.** `uv` lives at `~/.local/bin/uv` (pipx-installed) and is absent from
non-login shell PATH, so tooling must either use a login shell or prepend
`~/.local/bin`.
**Future.** Fine as-is; a Dockerfile would make the demo even more reproducible.

### N-002 — Chargeback reason codes were not in the API docs
**Problem.** The Disputes entity docs name a `reason_code` field but do not
enumerate its values, and `/docs/payments/disputes/chargeback-reason-codes/`
returns 404.
**Attempted.** Fetching several docs paths; all 404 or silent on codes.
**Root cause.** Razorpay publishes the code list as a PDF on their CDN, not as
an HTML docs page.
**Solution.** Pulled the authoritative list from
`https://cdn.razorpay.com/files/chargeback_codes.pdf` and vendored it into
`docs/` (PDF + extracted text) so later phases use real network codes
(Mastercard 48xx, Visa, etc.) rather than invented ones.
**Future.** Map each code to its recommended evidence categories in Phase 5.

### N-003 — Razorpay has NO "create dispute" API  ← key architectural finding
**Problem.** Phase 2 needs dispute events, so we must know whether Test Mode
can produce them.
**Investigated.** The Disputes API reference lists exactly six operations:
fetch all, fetch one, fetch with expanded payment, accept, contest, and the
Documents upload used for evidence. Independently confirmed at the SDK level:
`razorpay.resources.Dispute` exposes `all`, `fetch`, `accept`, `contest` — and
no `create`. The product docs state a dispute "can be initiated by the issuing
bank" or "by the customer".
**Root cause.** A dispute is not a merchant-authored object. It is raised
against the merchant by the bank or cardholder, so no merchant-facing create
endpoint exists in either Live or Test Mode.
**Solution (decided).** Do not fake a Razorpay response. Instead:
  * use genuine Test Mode Orders/Payments for the real integration surface;
  * feed the workflow a clearly labelled **Simulated Test Dispute** built to
    the exact documented dispute-entity schema;
  * keep ingestion behind one interface so a real
    `payment.dispute.created` webhook can replace the simulator unchanged.
Every simulated dispute is flagged in the UI and in the DB. Nothing synthetic
is ever presented as real Razorpay data.
**Future.** If a Razorpay test account is ever provisioned with seeded
disputes, the same code path reads them via `dispute.fetch`.

### N-004 — Verifying auth without any credentials
**Problem.** Needed to prove the failure paths work before test keys existed.
**Solution.** Ran the client against the real `api.razorpay.com` with knowingly
invalid keys. Razorpay returned a genuine HTTP 401
(`BAD_REQUEST_ERROR / Authentication failed`), which the wrapper translated
into `RazorpayAuthError` with an actionable message and redacted key material.
This verifies reachability, the SDK wiring, and error handling — it does not
verify a working credential, which still needs real test keys.

### N-005 — `.env` used `NAME = value`, unparseable by the shell
**Problem.** `set -a; . ./.env` failed with *command not found: RAZORPAY_KEY_ID*,
though python-dotenv read the file fine.
**Root cause.** The file used `NAME = value` with spaces around `=`. dotenv
strips them; POSIX shells treat the line as a command.
**Solution.** Normalised to `NAME=value` in place (values untouched). Both
dotenv and `source` now work. Worth keeping normalised so curl-based debugging
and any future `docker --env-file` do not break.

### N-006 — Phase 1 verified against the live Test Mode API
Authenticated with `rzp_test_TX6E…`. Created real order
`order_TX6aeI9VxWxNPK` (₹25,000) and fetched it back. Observed:
  * `order` returns an undocumented-in-our-list `offer_id` field.
  * `payment.all` → `count=0`: **orders alone do not create payments.** A
    payment requires a checkout completed with a test card. Phase 2 must not
    assume an order implies a payment.
  * `GET /v1/disputes` → `200 {"entity":"collection","has_more":false}` — note
    there is **no `items` and no `count` key** when empty, unlike other
    Razorpay collections. Code must use `.get("items", [])` and never assume
    `count` exists.
  * `POST /v1/documents` → 400 *"The purpose field is required"*, i.e. the
    evidence-upload route is live in Test Mode.

### N-007 — Bad ids were reported as "Razorpay is down"  (bug, fixed)
**Problem.** `fetch_dispute("disp_doesnotexist123")` raised
`RazorpayUnavailable` — telling an operator to retry a request that can never
succeed.
**Root cause.** Two layers. (1) The Razorpay gateway does not route a
malformed id at all: it answers `404 {"message":"no Route matched with those
values"}` with no `error.code`. (2) The SDK classifies purely on the body's
`error.code` (`client.py:199-205`), so anything unrecognised hits a catch-all
`else: raise ServerError(msg)` — producing `ServerError('')`. Our wrapper
treated every `ServerError` as transient.
**Solution.** Razorpay ids are a prefix plus exactly 14 alphanumerics
(`order_TX6aeI9VxWxNPK`, `disp_AHfqOvkldwsbqt`), so ids are now validated
locally before any network call. `ServerError` was removed from the transient
tuple and is handled explicitly: an empty message is reported as
*unrecognised — resource may not exist, or Razorpay may be degraded; route to
manual review* rather than a false promise that retrying helps.
**Verified.** Malformed ids reject locally with no network call; a
well-formed-but-absent id (`disp_AHfqOvkldwsbqt`) reaches Razorpay and returns
`RazorpayRequestError`.

### N-008 — The configured Groq model did not exist  (caught before Phase 4)
**Problem.** `AI_MODEL` was `llama-3.3-70b-versatile`, taken from Groq's docs
page. It is absent from this account's `GET /v1/models`; every call would have
failed at runtime.
**Root cause.** Trusted a docs page over the account's live model list.
**Solution.** Query `/openai/v1/models` and benchmark the real candidates.

### N-009 — The model inverted the recommendation label
**Problem.** `gpt-oss-20b` returned NO_CASE for a case with signed delivery
plus a written admission of receipt, and STRONG_CASE for a never-shipped order
— exactly backwards. Its `reasoning` array was correct in both.
**Root cause.** Not a reasoning failure: the enum is ambiguous out of context.
`STRONG_CASE` can be read as "the customer's claim is strong". The prompt never
said the label describes the *merchant's defence*.
**Solution.** The system prompt now defines each label explicitly in terms of
the merchant's position. Both models then classified the clear-cut cases
correctly. **Lesson for Phase 4: never ship a bare enum to the model — define
every value.**

### N-010 — Model selection, decided by measurement
Benchmarked on an ambiguous case (courier scanned to hub, no recipient proof),
temperature 0, strict `json_schema`, 6 runs each:

| Model | Schema valid | Latency | Notes |
|---|---|---|---|
| `openai/gpt-oss-20b` | **0/6** | — | drops required fields on ambiguous input |
| `openai/gpt-oss-120b` | **6/6** | 1.53 s | correct on all clear-cut cases |
| `qwen/qwen3.6-27b` | 0/6 | — | cannot satisfy the schema |

The small model reasons fine on easy cases but fails precisely where judgment
is needed, which is the whole point of this product. Chose
`openai/gpt-oss-120b`; 1.5 s is fine for a review dashboard.
Also confirmed `response_format=json_object` is **not** sufficient — it returns
valid JSON that ignores our field names. Strict `json_schema` is required.
Groq enforces the schema server-side and returns HTTP 400 on violation, which
validates the spec's "retry once, then fail safe to human review" design.

## Phase 2 — Dispute Ingestion

### N-011 — Razorpay documents six dispute events, spec listed four
While building the parser against the real webhook payload page, found
`payment.dispute.under_review` and `payment.dispute.action_required` in
addition to `created`/`won`/`lost`/`closed`. `under_review` maps cleanly onto
the documented `status` enum; `action_required` does not correspond to any
documented status value, so it is accepted and logged but deliberately never
written into the `dispute_status` field — inventing a status Razorpay doesn't
document would violate our own "don't fabricate API values" rule.
`dispute_schema.DISPUTE_EVENTS` carries all six.

### N-012 — Validated the parser against Razorpay's own sample payload
`parse_webhook_envelope` is tested against the exact JSON sample from
`https://razorpay.com/docs/webhooks/disputes/` (fetched, reproduced verbatim
in `tests/test_webhook_handler.py`), not a hand-built approximation.

### N-013 — Self-signed local test payloads land in the DB as `razorpay_webhook`
Running `scripts/send_test_webhook.py` against the live server produces a
case row with `source="razorpay_webhook"`, `is_simulated=False` — because a
valid HMAC signature is, by design, our only signal that a payload really
came from Razorpay, and locally we hold the same secret Razorpay would use.
This is correct trust-model behaviour, not a bug, but it means verification
runs must not be left in the demo database (a viewer would see fabricated
data labelled as genuine). Wiped `data/merchant/cases.db` after the Phase 2
verification run; worth remembering before any future ad-hoc webhook test.

### N-014 — Server didn't self-heal after its own DB file was deleted mid-run
Found while live-testing the Phase 2 webhook over the real ngrok tunnel (not
during initial Phase 2 development, but during the later live end-to-end
verification pass): deleting `cases.db` for cleanup while `uvicorn` was still
running caused every subsequent request to 500 with
`sqlite3.OperationalError: no such table: webhook_events`, because
`_get_settings()` only calls `init_case_db()` once, the first time it's
invoked, and caches the settings object forever after. Fixed by calling
`database.init_case_db()` (idempotent `CREATE TABLE IF NOT EXISTS`, cheap) on
every request in `webhook_handler.py`, so the server self-heals instead of
requiring a manual restart. Verified via the real tunnel: same test
sequence, no 500 this time.

## Phase 3 — Merchant Database + Seed Data

### N-015 — `get_scenarios()` returned different ids on every call
Reused `dispute_schema.generate_simulated_id()` (Phase 2, refactored out of
`dispute_simulator._sim_id` into a shared helper for Phase 3) to mint
`sim_order_`/`sim_pay_` ids for each seed scenario. Caught by
`test_seed_scenarios_insert_cleanly_into_a_fresh_db`: the test seeded the DB
with one call to `get_scenarios()`, then looked up "ORD-1001" via a second
call - and got `None`, because each call to `_build_scenarios()` mints fresh
random ids, so "ORD-1001" meant a different `payment_id` on each call within
the same process. Fixed by memoizing `get_scenarios()` (module-level cache,
built once per process). This was a real reproducibility bug, not just a test
artifact - any later phase that seeds a case with one call and looks it up
with another would have hit the identical silent failure.

## Phase 4 — AI Investigation Agent

### N-016 — Evidence citations are verified, not trusted
"The AI must never invent evidence" cannot be enforced by prompt text alone.
Implemented as a code-level guarantee instead: the prompt hands the model an
explicit list of citable references (`document:7`, `communication:12`,
`shipment:ORD-1001`, ...), every citation it returns must appear in that
whitelist, and a response citing anything else is rejected outright and
retried. A `STRONG_CASE` with zero citations is also rejected - a defensible
case must be able to point at something. Verified against the live model as
well as mocks (`tests/test_investigation_live.py`).

### N-017 — Communication/document row ids had to be exposed for traceability
`merchant_db.Communication` and `EvidenceDocument` are 0..n per order but
didn't expose their SQLite row `id`, so the AI could only have cited them
generically ("customer_communication") rather than pointing at one specific
message. Added `id` as a trailing optional field on both dataclasses
(populated on read), which keeps every existing positional constructor call
in the Phase 3 seed data and tests working unchanged.

### N-018 — The ambiguous scenario: model disagrees with our dev label
ORD-1003 (courier scanned to a city hub only, no recipient confirmation) is
labelled `WEAK_CASE` in our seed data; the live model consistently returns
`NO_CASE` at ~0.95 confidence. Reviewing its reasoning, the model is arguably
more right than the label: it identified the internal contradiction
(`delivery_status='delivered'` while the document shows only hub arrival) and
cited the merchant's OWN terms_and_conditions, which state risk of loss
passes only on *signed* delivery confirmation - absent here. We deliberately
did NOT tune the prompt to force agreement; that would be fitting the model
to an arbitrary label rather than to the evidence. Flagged for Phase 8, where
label quality gets scrutinised properly against a real held-out set.

## Phase 5 — Evidence Builder + Document Generation

### N-019 — Evidence categories are populated from records, never padded
Razorpay exposes eleven evidence categories. `select_evidence_categories()`
includes one only when a concrete merchant record backs it, and each entry
lists the references behind it so the package traces back to source rows.
Two consequences worth noting: a `never_shipped` shipment does NOT become
`shipping_proof` (offering it would actively damage the merchant's case, so
it becomes a warning instead), and a refund only counts as
`refund_confirmation` when its status is actually `processed` - a pending
refund proves nothing. A digital product with no shipment produces no
shipping category and no warning, because absence is expected there; a
physical one with no shipment produces a warning.

### N-020 — NO_CASE refuses to build a contest package by default
`build_evidence_package()` raises `EvidenceBuildError` when the investigation
concluded NO_CASE. Producing a polished, submittable-looking package for a
case the investigation says cannot be won is exactly how a human reviewer
ends up rubber-stamping a bad contest. A reviewer can still override with
`force=True` (this is what the Phase 6 UI will call), and the override is
recorded in the package's own warnings so it is visible downstream.

### N-021 — Contest summary composed deterministically, not re-generated
The 1000-char summary is assembled from the ALREADY-VALIDATED investigation
text (executive summary + verified citation references) rather than by asking
the model to write a fresh one. The investigation's claims have already been
checked against real records, so reusing them cannot introduce new
hallucinations; a second free-form generation could. The model is only
invoked if the composed text exceeds the limit, and purely to compress -
with the result re-validated and truncated programmatically if it still
doesn't fit.

## Phase 6 — Human Review Dashboard

### N-022 — Deadline thresholds finally wired up
`DEADLINE_CRITICAL_HOURS` / `DEADLINE_WARNING_HOURS` were added to config in
Phase 1 and had been dead configuration ever since — nothing read them.
`review_workflow.deadline_status()` now uses them to classify each case as
EXPIRED / CRITICAL / WARNING / NORMAL, which drives the queue's deadline
column, the overview's "approaching deadline" metric, and an explicit
"DEADLINE EXPIRED — cannot be contested" banner on the case page (spec
section 15).

### N-023 — "Request further review" records without inventing a state
The dashboard offers three human actions, but the spec's state machine only
has APPROVED and OVERRULED out of PENDING_HUMAN_REVIEW. Rather than invent a
state that no existing transition expects, "request further review" writes an
audit entry (`human_request_further_review`) with previous_state ==
new_state, leaving the case in PENDING_HUMAN_REVIEW where it already
correctly sits. The request is fully visible in the audit log without
distorting the state machine.

### N-024 — Every audit entry records the AI recommendation next to the human decision
`record_human_decision()` writes a reason of the form "AI recommended X;
human Y. <note>". Without this, an audit log showing only `OVERRULED` loses
the single most interesting fact — that a human looked at a STRONG_CASE
recommendation and disagreed with it. There is a test asserting both halves
are present.

### N-025 — Citations are resolved back to records in the UI
The AI's evidence citations are rendered as expanders that show the ACTUAL
record behind each reference (`resolve_citation()`): the customer's message
text for a `communication:N`, the courier fields for a `shipment:`, the
policy body for a `policy:`. A reviewer never has to take a cited claim on
trust, and an unresolvable citation renders as an explicit warning rather
than being quietly dropped.

## Phase 7 — Razorpay Contest Integration

### N-026 — Simulated disputes are blocked in the backend, not the UI
`contest_service.assert_submittable()` raises `SubmissionBlocked` for any case
with `is_simulated=True` or a source other than `razorpay_webhook`, and it is
called at the top of the shared `_send_contest()` path used by BOTH draft and
submit. Verified by test that the mocked Razorpay client records zero calls
when a simulated case is pushed at it. The dashboard also displays the block,
but the block does not depend on the dashboard.

### N-027 — Three contest operations, deliberately separated
`build_local_draft()` never touches the network and always works — that is
what a reviewer inspects, and it lets a simulated case be demonstrated end to
end. `save_draft_to_razorpay()` is `action="draft"` (evidence stored at
Razorpay, not sent to the bank). `submit_contest()` is `action="submit"` and
requires `human_confirmed=True` plus a named actor; there is no default that
permits submission. A test asserts every `submit_contest` call in the
dashboard passes `human_confirmed=True` and sits behind a confirmation
checkbox.

### N-028 — State advances only after Razorpay confirms
`_send_contest()` records the attempt and re-raises on any Razorpay error;
the `transition_case_state()` call happens only after a successful response.
A failed draft leaves the case at APPROVED, retryable, with the failure
recorded in `contest_attempts` for audit. Tested for both a contest-call
failure and a mid-upload failure.

### N-029 — Uploads are idempotent via a UNIQUE(dispute_id, local_path) index
Every successful upload is persisted immediately with its `doc_...` id, so a
retry after a partial failure reuses what was already uploaded rather than
creating duplicate documents at Razorpay. Verified: draft-then-submit for one
case performs three uploads total, not six.

## Post-Phase-7 — UX pass and Indian-market grounding (from live dashboard use)

### N-030 — Investigation is now automatic, never a manual per-case click
Removed the "Run AI investigation" button entirely. A case with no saved
result is investigated the moment it's opened (spinner shown, no click), and
`advance_to_review()` now runs immediately after a successful investigation
rather than being deferred until a human clicks Approve/Reject — otherwise
the new workflow stepper would show "Ingested" as current while the AI
verdict was already on screen, contradicting itself. `scripts/seed_merchant_db.py`
also investigates every scenario by default (`--no-investigate` to skip), and
the dashboard sidebar has a bulk "Investigate all pending" action for
anything that reaches it uninvestigated (e.g. a real webhook case). A human's
only required action is now Approve / Reject / Request further review.
**Test hazard avoided:** `AppTest` execs `dashboard/app.py` directly rather
than importing it as a real Python module, so `dashboard.app` never appears
in `sys.modules` - patching `"dashboard.app.investigate_dispute"` in a test
silently does nothing. The correct target is the real module
(`"src.investigation_agent.investigate_dispute"`), which dashboard's
`from ... import` re-resolves fresh on every `AppTest.run()`.

### N-031 — Two more seed scenarios, grounded in Indian merchant/logistics reality
`ORD-1008` (UPI payment shown as "failed" by the customer's banking app while
actually captured — the single most common dispute pattern on Indian payment
rails, since UPI dominates Razorpay's own transaction volume) and `ORD-1009`
(RTO — return to origin — with genuine courier NDR/non-delivery-report
attempts logged, still undelivered and unrefunded; the defining operational
pain point of Indian D2C logistics). Added `Scenario.dispute_payment_method`
(default `"card"`) so a scenario can specify `"upi"`, wired through to
`SimulatedDisputeSpec.payment_method`. `RTO` needed no schema change -
`returned_to_sender` already existed in `merchant_db.DELIVERY_STATUSES`.
Both investigated live: ORD-1008 → STRONG_CASE (85%), ORD-1009 → WEAK_CASE
(60%, a genuine judgment call, as intended).

### N-032 — Integration proof moved from chat into the dashboard itself
Evaluators shouldn't have to read `BUILD_LOG.md`/`NOTES.md` to know what's
genuinely proven against the live Razorpay API vs. verified against the
documented contract. Added `render_integration_proof()` to the dashboard —
visible immediately below the header, before anything else — which states
both plainly and includes a live, on-demand button: **"Upload a real
evidence document to Razorpay now."** Insight that made this possible:
`POST /v1/documents` needs no existing dispute, only valid credentials, so
document upload (unlike the final `contest` call) genuinely can be proven
live without ever creating a test dispute - which nothing can do, including
Razorpay itself. Verified against the real API: returned a genuine
`doc_TXTlCx4drZ3vSD`.
