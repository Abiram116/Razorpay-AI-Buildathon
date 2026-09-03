# Build Log — AI Chargeback Defense Agent

This is the project's permanent development diary. Every meaningful problem,
API limitation, unexpected behavior, or design decision forced by a real
constraint gets an entry here, in the order it happened. Small/cosmetic
issues are not logged here — see `NOTES.md` for the full engineering log,
including minor items. This file is for what's worth talking about in a
submission.

---

## Submission-Worthy Challenges

*(Promoted here only after the problem and fix were actually verified.)*

1. **Razorpay has no "create dispute" API — in Live or Test Mode.** This
   forced an architectural decision early: real Test Mode Orders/Payments for
   the genuine Razorpay integration surface, plus a clearly labelled
   simulated dispute for the workflow, behind one interface a real webhook can
   replace unchanged. See entry 2026-09-02-01.
2. **The AI inverted its own recommendation despite correct reasoning.**
   `STRONG_CASE`/`NO_CASE` are ambiguous out of context — the model read
   `STRONG_CASE` as "the customer's claim is strong" rather than "the
   merchant's defence is strong," and returned NO_CASE for a case with signed
   delivery + a written admission of receipt (backwards). Caught by testing
   against a scripted ground-truth case before Phase 4 was even built, not by
   accident during a demo. See entry 2026-09-02-03.
3. **A wrapper bug told operators to retry a request that could never
   succeed.** A malformed dispute ID was misclassified as "Razorpay
   unavailable, retry" because the SDK collapses unclassifiable gateway
   responses into the same exception type as real server outages. See entry
   2026-09-02-02.
4. **A live end-to-end test through a real public tunnel caught a
   self-healing gap a purely local test never would have.** Deleting the
   database file while the server kept running left every request 500ing
   until a manual restart, because schema initialization was cached forever
   instead of being cheaply re-verified. See entry 2026-09-02-05.
5. **Seed "data" that silently wasn't deterministic.** The demo scenario
   generator minted a fresh random Razorpay-shaped id on every call, so the
   same named order ("ORD-1001") meant different underlying data depending
   on how many times the function had been called that process — caught by
   a test, not by inspection. See entry 2026-09-02-06.
6. **Enforcing "the AI must not invent evidence" in code rather than in the
   prompt.** Every citation the model returns must resolve against a
   whitelist of references actually present in the evidence bundle it was
   given; anything else is a hallucination and the response is rejected and
   retried. Verified against the live model, not just mocks. See `NOTES.md`
   N-016.
7. **A rate limit is not an outage.** Groq free-tier 429s were being handled
   identically to a permanently invalid API key, sending recoverable cases to
   manual review when waiting 7 seconds would have succeeded — the exact
   manual work this product exists to eliminate. See entry 2026-09-02-08.
8. **A cache that was never needed broke test isolation invisibly.** Fourteen
   dashboard tests failed as a suite and passed individually, because
   `@st.cache_resource` on cheap config outlived each test run and handed
   later tests a previous test's database paths. See entry 2026-09-02-11.
9. **Two layers each had a correct-but-different idea of "the evidence
   package", and the gap was invisible until they met.** Records for a human
   to read vs. files an API will accept — three cited evidence categories
   would have silently vanished from the Razorpay payload. See entry
   2026-09-02-12.
10. **A regression test for one fix found a second, unrelated bug of the
    same shape on its first run.** Written broadly ("no evidence category may
    go unsent") rather than narrowly ("billing_proof must be sent"), the test
    for the billing_proof fix immediately caught that customer_communication
    had the identical gap, masked in the demo data by a coincidence. See
    entry 2026-09-03-01.
11. **LLM output is not ASCII, and it broke the human-facing deliverable.**
   Groq's reasoning contained typographic Unicode (non-breaking hyphens, en
   dashes, narrow spaces) outside reportlab's Latin-1 font range, rendering
   as literal black boxes in the PDF a reviewer has to read and approve. The
   diagnostic scan was itself misleading at first, because `json.dumps`
   hides non-ASCII behind `\uXXXX` escapes. See entry 2026-09-02-10.

---

## 2026-09-02-01 — Phase 1 — Razorpay has no merchant-facing "create dispute" endpoint

**Problem / obstacle.** Phase 2 (dispute ingestion) requires knowing whether
Razorpay Test Mode can produce dispute events I could build and demo
against. If it can't, the entire ingestion design changes.

**What caused it.** A dispute is not a merchant-authored object — it is
raised against the merchant by the issuing bank or the cardholder. Razorpay
does not expose a way for a merchant to originate one via API, in Live mode
or Test mode.

**How I investigated it.** Two independent checks: (1) read the official
Disputes API reference — it lists exactly six operations (fetch-all, fetch,
fetch-with-expanded-payment, accept, contest, plus the Documents upload used
for evidence) and no create; (2) inspected the installed `razorpay` Python
SDK directly — `razorpay.resources.Dispute` exposes only `all`, `fetch`,
`accept`, `contest`, confirmed via `inspect.getmembers`. The product docs
independently confirm disputes are "initiated by the issuing bank" or "by the
customer."

**Solution / fix.** Do not fake a Razorpay API response. Instead: use genuine
Test Mode Orders/Payments for the real Razorpay integration surface, and feed
the AI workflow a schema-faithful, explicitly labelled "Simulated Test
Dispute" for the parts Razorpay itself cannot produce in test mode.

**What I changed in the code.** `src/razorpay_client.py` deliberately has no
`create_dispute()` method (documented in a code comment so the omission
reads as intentional, not missed). Ingestion is designed around one shared
interface so a real `payment.dispute.created` webhook can replace the
simulator later without changing downstream code (Phase 2).

**What I learned.** Don't assume a sandbox mirrors every real-world actor.
Test Mode gives you the merchant-controllable half of the system; it cannot
simulate a bank or a cardholder's decision to dispute a charge.

**Status.** Resolved — architecture decided and documented; simulator to be
built in Phase 2.

---

## 2026-09-02-02 — Phase 1 — Error wrapper told operators to retry an unrecoverable failure

**Problem / obstacle.** While probing error paths ahead of Phase 2,
`fetch_dispute("disp_doesnotexist123")` raised my own `RazorpayUnavailable`
error — "Unable to reach Razorpay... Retry, or route to manual review." That
is actively wrong: a malformed/nonexistent ID will never succeed no matter
how many times it's retried.

**What caused it.** Two stacked issues. (1) Razorpay's API gateway does not
route a malformed dispute ID at all — it returns a bare `404
{"message":"no Route matched with those values"}` with no `error.code` field.
(2) The Razorpay Python SDK classifies errors purely by the response body's
`error.code` (`client.py`), so any response it doesn't recognize — including
that bare 404 — falls into a catch-all `else: raise ServerError(msg)`,
producing an empty-message `ServerError`. My own wrapper then treated every
`ServerError` as a transient, retry-worthy failure.

**How I investigated it.** Reproduced the exact failure with `curl` directly
against `api.razorpay.com` using real (redacted) test credentials, confirmed
the raw 404 body, then read the installed SDK's `client.py` request-handling
branch line by line to see exactly which `error.code` values map to which
exception class.

**Solution / fix.** Validate Razorpay entity IDs locally (documented format:
prefix + exactly 14 alphanumeric characters, confirmed against real IDs like
`order_TX6aeI9VxWxNPK`) before any network call, so malformed IDs never reach
the network. For genuine `ServerError`s from Razorpay, stopped treating them
as automatically transient — an empty-message `ServerError` is now reported
as "unrecognised response — resource may not exist, or Razorpay may be
degraded; route to manual review," instead of promising a retry will help.

**What I changed in the code.** `src/razorpay_client.py`: added
`validate_entity_id()`, removed `ServerError` from the transient-error tuple,
added an explicit `except ServerError` branch with honest messaging.

**What I learned.** Don't trust an SDK's exception hierarchy to reflect the
real failure mode — read the classification logic itself when the stakes are
"tell a human whether retrying is worth their time."

**Status.** Resolved and verified: malformed IDs now reject locally with zero
network calls; a well-formed-but-absent ID correctly reaches Razorpay and
returns a distinct "request rejected" error instead of "service down."

---

## 2026-09-02-03 — Phase 1 (pre-Phase-4 spike) — Configured Groq model didn't exist; smaller model inverted its own verdict

**Problem / obstacle.** Two compounding problems surfaced while spiking the
AI investigation call ahead of Phase 4: (a) the model ID I'd configured,
`llama-3.3-70b-versatile`, does not exist on this Groq account — every AI
call would have failed outright; (b) once a real model list was queried and a
smaller candidate (`openai/gpt-oss-20b`) tested against scripted evidence, it
returned **NO_CASE** for a case with signed courier delivery plus the
customer's own written message "I received the package" — and **STRONG_CASE**
for an order that was never shipped at all. Exactly inverted. Its
step-by-step `reasoning` text was factually correct in both cases; only the
final label was backwards.

**What caused it.** (a) Groq's public docs page listing model IDs was stale
relative to what this account actually serves — trusted documentation over
the live account. (b) The label inversion was a prompt design flaw, not a
reasoning failure: `STRONG_CASE` / `NO_CASE` are ambiguous without saying
*whose* position is strong. The model read `STRONG_CASE` as "the customer's
claim is strong" rather than "the merchant's defence is strong."

**How I investigated it.** Queried `GET /openai/v1/models` directly against
the account's real API key to get the actual served model list. Then ran a
small structured-output benchmark: three scripted cases with known correct
verdicts (clear delivery-confirmed, clear never-shipped, and one genuinely
ambiguous partial-tracking case), each run through candidate models at
temperature 0 with a strict JSON schema, checking both label correctness and
whether the response validated against my schema at all.

**Solution / fix.** Rewrote the system prompt to define each recommendation
value explicitly in terms of the merchant's defence ("STRONG_CASE = merchant
records affirmatively disprove the customer's claim," etc.) rather than
leaving the enum to be inferred. Re-ran the benchmark: both `gpt-oss-20b` and
`gpt-oss-120b` then classified the clear-cut cases correctly. But a second,
independent problem showed up on the ambiguous case: `gpt-oss-20b` failed
Groq's server-side schema validation **6 out of 6 times** (dropping required
fields), while `gpt-oss-120b` passed **6 out of 6** at ~1.5s latency. Selected
`gpt-oss-120b` as the primary model precisely because the smaller model's
failures concentrated on the hard cases — the ones this product exists to get
right — with `gpt-oss-20b` kept only as a documented fallback.

**What I changed in the code.** `src/config.py`: `AI_MODEL` corrected to
`openai/gpt-oss-120b` with `AI_MODEL_FALLBACK = openai/gpt-oss-20b` and the
benchmark numbers recorded in a comment; `AI_RESPONSE_FORMAT` pinned to
`json_schema` (confirmed `json_object` mode returns valid JSON that ignores
my field names entirely — not a safe substitute).

**What I learned.** Never ship a bare enum to an LLM — every value needs an
explicit definition in the prompt, or the model will pick a plausible but
wrong interpretation and reason confidently within it. Also: model choice
must be decided by testing against the account's real, current capabilities
and against deliberately hard/ambiguous inputs — not by picking the biggest
name from documentation, and not by testing only easy cases where every
candidate looks fine.

**Status.** Resolved for the cases tested. Still open: this was a 3-case
spike, not the full held-out evaluation set (Phase 8) — the model choice
should be re-confirmed once that larger evaluation exists.

## 2026-09-02-04 — Phase 2 — Razorpay's webhook envelope carries no delivery/event id

**Problem / obstacle.** A webhook receiver needs to handle duplicate
deliveries safely (Razorpay retries on any non-2xx response, and an operator
may replay a delivery manually). The standard way to do that is to dedupe on
a unique id the sender includes with each delivery.

**What caused it.** Razorpay's documented webhook envelope
(`{entity, account_id, event, contains, payload, created_at}` — verified
against the real `payment.dispute.created` sample) has no delivery id, no
event id, and no idempotency key of any kind. `created_at` is only
second-resolution and is not guaranteed unique across events.

**How I investigated it.** Fetched and read the actual documented sample
payload rather than assuming a Stripe-style `evt_...` id would be present;
confirmed no such field exists anywhere in the envelope or nested payment/
dispute objects.

**Solution / fix.** Deduplicate on a SHA-256 hash of the raw request body
itself. Two deliveries of the literal same event produce byte-identical
bodies (same JSON, same field ordering, same values) and therefore the same
hash; a genuinely different event (even for the same dispute — e.g. `created`
then later `won`) produces a different body and is processed as a new event,
which is the correct behaviour.

**What I changed in the code.** `src/database.py`: a `webhook_events` table
keyed on `body_hash TEXT PRIMARY KEY`; `hash_webhook_body()` in the same
module; the webhook handler computes the hash before any processing and
short-circuits to `{"status": "duplicate_ignored"}` on a repeat.

**What I learned.** Don't assume a well-known API pattern (idempotency
keys) is present just because it's common — Razorpay's dispute webhooks don't
have one, so idempotency has to be derived from the payload itself.

**Status.** Resolved and verified: sending the identical signed payload twice
to the running server processes it once (audit log shows exactly one
`ingest` entry) and acknowledges the second delivery without reprocessing.

## 2026-09-02-05 — Phase 2 (live verification pass) — Server didn't self-heal after its own database file disappeared

**Problem / obstacle.** While proving the Phase 2 webhook pipeline end to
end through a real public ngrok tunnel (not the earlier local-only tests),
deleting `data/merchant/cases.db` between test runs — routine cleanup, to
keep fabricated test data out of what would later be the demo database —
caused every subsequent request to the running server to fail with HTTP 500
`sqlite3.OperationalError: no such table: webhook_events`, instead of simply
recreating the file.

**What caused it.** `webhook_handler.py`'s `_get_settings()` calls
`database.init_case_db()` exactly once, the first time it's invoked, then
caches the `Settings` object at module scope. Once the schema had been
created for the *old* `cases.db`, deleting that file left the server holding
a path to a file that no longer had any tables, with nothing to make it
notice or recover.

**How I investigated it.** Reproduced with a genuinely signed webhook
delivery to the live server (not a mock), read the actual traceback in the
server's own log rather than guessing, and traced it directly to the
`_get_settings()` caching logic.

**Solution / fix.** Run `database.init_case_db()` — an idempotent
`CREATE TABLE IF NOT EXISTS`, cheap even on a hot path — on every request
instead of once at process start, so the server self-heals if its storage is
ever reset out from under it rather than requiring a manual restart.

**What I changed in the code.** `src/webhook_handler.py`:
`receive_dispute_webhook` now calls `database.init_case_db(settings.paths.case_db)`
at the top of every request.

**What I learned.** Caching an expensive setup step is usually right, but
caching "I already made sure the database exists" forever is a silent
single-point-of-failure the moment anything external can change that
database's file. Re-verifying is often cheap enough to just always do.

**Status.** Resolved and verified: repeated the exact same live-tunnel test
sequence after the fix (fresh signed delivery, duplicate delivery, bad
signature) with the database deleted beforehand — no 500, correct behaviour
throughout.

## 2026-09-02-06 — Phase 3 — Seed data generator was non-deterministic across calls

**Problem / obstacle.** A test that seeded the merchant DB from
`get_scenarios()` and then looked up order "ORD-1001" from a *second* call to
`get_scenarios()` got `None` back — the order that had just been inserted
appeared not to exist.

**What caused it.** `get_scenarios()` calls `_build_scenarios()`, which mints
a fresh `sim_order_.../sim_pay_...` id via `generate_simulated_id()` (random
suffix) for every scenario, every time it's called. Two calls to
`get_scenarios()` in the same process therefore describe two different
datasets that merely share order labels like "ORD-1001" — the underlying
`payment_id` differs each time, so a lookup by `payment_id` genuinely
couldn't find what a different call had inserted.

**How I investigated it.** The failing test's traceback pointed straight at
`get_case_evidence()` returning `None`; added a one-off comparison of
`scenario.order.payment_id` across two successive `get_scenarios()` calls,
which confirmed the ids differed every time — not a lookup bug, a data
generation bug.

**Solution / fix.** Memoized `get_scenarios()` with a module-level cache so
it builds the scenario list once per process and returns the same objects
(and therefore the same ids) on every subsequent call.

**What I changed in the code.** `src/merchant_seed_data.py`:
`_CACHED_SCENARIOS` module global, `get_scenarios()` now populates it once
and returns it thereafter.

**What I learned.** A "seed data" function that regenerates random
identifiers on every call isn't actually a stable dataset — it just looks
like one until something calls it twice. This would have surfaced later as a
confusing, intermittent failure in Phase 4/5/6 (whichever one happened to
call `get_scenarios()` a second time to cross-reference a case) if the test
suite hadn't caught it now, at the source, with an obvious cause.

**Status.** Resolved and verified — full test suite (51 tests) passes,
including a test that explicitly seeds from one call and reads back from
another.

## 2026-09-02-07 — Phase 4 — Seed identifiers weren't stable across processes (the deeper half of 2026-09-02-06)

**Problem / obstacle.** The first live run of the investigation agent
crashed immediately: `cases.get(payment_id)` returned `None` for every
scenario, even though `scripts/seed_merchant_db.py` had just seeded all seven
cases successfully moments earlier.

**What caused it.** Entry 2026-09-02-06 fixed `get_scenarios()` returning
different ids on repeated calls by memoizing it — but memoization is
per-process. The seeding script and the investigation run are *different
processes*, so each one minted its own fresh batch of random
`sim_order_/sim_pay_` ids via `generate_simulated_id()`. The database
contained one set of ids; the investigating process was looking up an
entirely different set. The earlier fix had masked the symptom within a
single process without addressing the underlying issue: the identifiers were
random in the first place.

**How I investigated it.** Printed `ORD-1001`'s `payment_id` from two
separate Python processes and compared — they differed, confirming the ids,
not the lookup, were the problem.

**Solution / fix.** Added `derive_simulated_id(prefix, key)`, which derives
the id from a SHA-256 of a stable key (the `merchant_order_id`) instead of
randomness, and switched the seed data to it. "ORD-1001" now names exactly
one `payment_id` in every process, on every run, forever. The random
`generate_simulated_id()` remains for ad-hoc simulation
(`scripts/run_simulator.py`), where a genuinely new dispute each run is the
intent.

**What I changed in the code.** `src/dispute_schema.py`: added
`derive_simulated_id()` alongside the existing random generator, with the
distinction documented. `src/merchant_seed_data.py`: `_order_ids()` now takes
the merchant order id and derives from it.

**What I learned.** The first fix treated the symptom (unstable within a
call) rather than the cause (identifiers were random at all). "Seed data"
implies reproducibility by definition — if re-running the seeder changes what
the data *is*, it isn't a fixture, and nothing downstream (evaluation in
Phase 8 especially) can rely on it.

**Status.** Resolved and verified — ids confirmed identical across separate
processes; full seed → investigate flow works end to end.

## 2026-09-02-08 — Phase 4 — Groq free-tier rate limits were being treated as permanent failures

**Problem / obstacle.** Investigating all seven seeded scenarios in sequence,
the fifth case (ORD-1005) failed with `AI_UNAVAILABLE` and was routed to
manual review. The underlying error was an HTTP 429 from Groq:
*"Rate limit reached ... on tokens per minute (TPM): Limit 8000, Used 6227,
Requested 2779. Please try again in 7.545s."*

**What caused it.** Two things. (1) The free tier's 8,000 tokens-per-minute
budget is genuinely small relative to these prompts — a full evidence bundle
plus policies runs roughly 2–3k tokens per case, so a handful of consecutive
investigations exhausts it. (2) More importantly, my error handling
collapsed *every* exception from the Groq client into a single
`GroqUnavailable`, so a temporary "try again in 7.5 seconds" was handled
identically to a permanently invalid API key — the case was abandoned to
manual review when simply waiting would have succeeded.

**How I investigated it.** Read the actual 429 body rather than just the
exception type, which showed both the TPM budget and an explicit retry delay;
then inspected `groq`'s exception hierarchy to see which errors are
meaningfully retryable (`RateLimitError`, `APITimeoutError`,
`APIConnectionError`, `InternalServerError`) versus permanent
(`AuthenticationError`, `BadRequestError`).

**Solution / fix.** Split the failure taxonomy: a new `GroqTransientError`
carries a `retry_after`, parsed from the API's own message, and is retried
with backoff up to `MAX_TRANSIENT_RETRIES`; permanent errors still fail fast.
Exhausting the retries still converts to `GroqUnavailable`, so the case
continues to fail *safe* rather than hanging or being dropped.

**What I changed in the code.** `src/investigation_agent.py`: added
`GroqTransientError`, `_RETRY_AFTER_PATTERN`, and
`_call_groq_with_backoff()`; `_call_groq()` now maps Groq's exception types
onto transient-vs-permanent instead of catching bare `Exception`.

**What I learned.** "The API call failed" is not one failure mode. Treating a
rate limit like an outage silently converts a recoverable delay into a case a
human has to pick up by hand — the exact manual work this product exists to
remove. Also worth flagging forward: at 8,000 TPM the Phase 8 evaluation run
(200 cases) will be rate-limit-bound and needs pacing built in, not just
retries.

**Status.** Resolved and verified — re-ran all seven scenarios; the rate
limit was hit again, backed off automatically, and ORD-1005 completed
successfully instead of failing.

## 2026-09-02-09 — Phase 5 — Merchant evidence files were in a format Razorpay cannot accept

**Problem / obstacle.** Phase 3 seeded merchant evidence records as `.txt`
files on disk. Razorpay's Documents API (`POST /v1/documents`,
`purpose=dispute_evidence`) accepts **only PDF, PNG and JPG** — verified in
Phase 1. So not one of the merchant's actual evidence files could ever be
uploaded as-is, and a contest submission requires at least one document id.

**What caused it.** Phase 3 stored evidence in whatever format was convenient
for seeding, without cross-checking against the upload constraint that had
already been documented in Phase 1. The two facts existed in the project but
had never been put next to each other.

**How I investigated it.** Compared `file data/merchant/documents/*.txt`
against the format constraint recorded in the README's Razorpay integration
table, before writing any of the Phase 5 upload path.

**Solution / fix.** Built `src/document_generator.py` to render each merchant
record into an uploadable PDF, embedding the source file's contents verbatim
where the file exists — and stating explicitly that the source was not found
where it doesn't, rather than silently producing a PDF that implies a
document was read.

**What I changed in the code.** New `src/document_generator.py`
(`generate_evidence_document_pdf`, `generate_explanation_letter_pdf`,
`generate_case_report_pdf`); `evidence_builder.build_evidence_package()`
renders every merchant document and marks the package non-submittable if none
could be produced.

**What I learned.** An external API's format constraints have to reach back
into how internal data is stored, not just how it's transmitted. This was
discoverable in Phase 3 — both facts were already written down in the repo —
but nothing forced them to be compared until the upload path was built.

**Status.** Resolved and verified — 14 valid PDFs generated across the seeded
cases, confirmed as real PDFs and content-checked by extracting their text.

## 2026-09-02-10 — Phase 5 — LLM output rendered as black boxes in the generated PDFs

**Problem / obstacle.** The first generated case report contained
`OTP■verified` where the AI's reasoning had said "OTP-verified" — a literal
black box in a document intended for a human reviewer to read and sign off on.

**What caused it.** reportlab's built-in fonts (Helvetica, Courier) are
limited to Latin-1/WinAnsi. Groq's output routinely contains typographic
Unicode that falls outside it. Measured across the actual stored
investigations: U+2011 (non-breaking hyphen) ×8, U+2013 (en dash) ×1, U+202F
(narrow no-break space) ×3. Each renders as a box glyph. I'd anticipated
this for the rupee sign (₹, U+20B9) and written amounts as "INR" text, but
only for my own strings — not for text the model generates.

**How I investigated it.** Extracted text from the generated PDFs and
flagged every codepoint above the font's range; then scanned the stored
investigation JSON for non-ASCII characters. The first scan came back empty
and was misleading — `json.dumps` defaults to `ensure_ascii=True`, so the
offending characters were sitting in the database as `\uXXXX` escapes.
Re-scanning the *parsed* JSON revealed them.

**Solution / fix.** Added `_to_renderable()` in the document generator: a
transliteration table maps the common typographic characters to ASCII
equivalents (dashes → `-`, smart quotes → `"`, ellipsis → `...`, ₹ → `INR `),
and anything still outside the font's range is dropped rather than left to
render as a box. Applied inside `_escape()`, so every string that reaches a
PDF goes through it.

**What I changed in the code.** `src/document_generator.py`:
`_UNICODE_FALLBACKS`, `_to_renderable()`, and `_escape()` now composes both.

**What I learned.** LLM text is not ASCII and should not be assumed to be —
it inherits typographic conventions from its training data. Any rendering
path that can't handle the full Unicode range needs an explicit
transliteration step. Also: when scanning stored JSON for encoding problems,
parse it first — `ensure_ascii` will hide exactly what you're looking for.

**Status.** Resolved and verified — re-generated every PDF and confirmed zero
unrenderable codepoints remain across all of them.

## 2026-09-02-11 — Phase 6 — Streamlit's resource cache silently broke test isolation

**Problem / obstacle.** The dashboard test suite behaved differently depending
on how it was run: 14 of 21 tests failed when the file ran as a whole, but
every one of them passed in isolation. A test that fails only in company is
usually a shared-state problem, not a logic problem.

**What caused it.** `get_settings()` in `dashboard/app.py` was decorated with
`@st.cache_resource`. Streamlit's resource cache is process-global and
deliberately outlives an individual script run, so the first `AppTest` in the
file populated it with a `Settings` pointing at that test's `tmp_path`, and
every subsequent test silently received the same stale object — reading a
database directory belonging to a finished test rather than its own.

**How I investigated it.** Ran one failing test alone (it passed), which
ruled out the assertion and pointed at cross-test state. Confirmed the cache
was the only process-global thing in the app, since each test otherwise
builds its own temp databases and patches `load_settings` per run.

**Solution / fix.** Removed the decorator. `load_settings()` reads `.env` and
builds a few frozen dataclasses — caching it saves nothing measurable, while
holding a stale `Settings` for the life of the process means the running app
would also ignore any configuration change until a full restart.

**What I changed in the code.** `dashboard/app.py`: `get_settings()` is now
a plain function, with a comment recording why it must not be cached.

**What I learned.** `@st.cache_resource` is for genuinely expensive, genuinely
immutable resources (a DB connection pool, a loaded model). Applying it to
cheap configuration trades a nonexistent performance win for real staleness —
and the staleness surfaced first as a confusing test-ordering bug rather than
as anything obviously cache-shaped.

**Status.** Resolved and verified — all 21 dashboard tests pass together and
individually; full suite 141 passed, 2 skipped.

## 2026-09-02-12 — Phase 7 — Half the evidence package could never have reached Razorpay

**Problem / obstacle.** Building the contest payload exposed a mismatch that
had been latent since Phase 5. `EvidencePackage.evidence_categories` looked
complete — it cited `refund_cancellation_policy ← policy:refund_policy`,
`term_and_conditions ← policy:terms_and_conditions`, and the explanation
letter existed as text. But Razorpay's contest fields take **uploaded
document ids**, not record references. A policy row in SQLite is not a file,
so those categories would have been silently dropped from the payload: the UI
would show a rich six-category package while only two categories actually
reached the bank.

**What caused it.** Phase 5 built the category map from merchant *records*,
which was the right model for showing a reviewer what evidence exists. It
rendered PDFs only for `evidence.documents`. The explanation letter was
generated by the *caller* (the CLI script and the dashboard) and never became
part of the package, and policies were never rendered at all. Nothing forced
the two representations to be reconciled until something had to serialise the
package into an actual API call.

**How I investigated it.** Built the payload from the package and compared
its keys against `evidence_categories` — three cited categories had no
corresponding uploadable file.

**Solution / fix.** Gave the contest service ownership of "what actually gets
uploaded": `collect_uploadable_documents()` joins the package's rendered
merchant documents with an explanation-letter PDF and a PDF per cited policy,
generating the missing ones on demand. Added `generate_policy_pdf()` to the
document generator. `ContestDraft` also reports `unsupported_categories`, so
anything still un-sendable is stated rather than quietly missing.

**What I changed in the code.** New `src/contest_service.py`; added
`generate_policy_pdf()` to `src/document_generator.py`. Phase 5's
`evidence_builder` was deliberately left untouched — its record-oriented view
is correct for review; the file-oriented view belongs to the layer that talks
to the API.

**What I learned.** "The evidence package is complete" meant two different
things in two layers — a set of records for a human to read, and a set of
files an API will accept. Both were correct in isolation; the gap only became
visible where they had to meet.

**Status.** Resolved and verified — a real-dispute payload now carries
`shipping_proof`, `explanation_letter` and `refund_cancellation_policy` with
genuine `doc_...` ids.

## 2026-09-03-01 — Phase 7 — Live dashboard use surfaced two more unsendable evidence categories

**Problem / obstacle.** Reviewing an actual case in the running dashboard
(`ORD-1001`, `STRONG_CASE`), the Contest Draft section showed: *"Cited but
not sendable as documents: `billing_proof`."* The evidence-category list
looked complete everywhere else, but this one line said otherwise.

**What caused it.** The same root cause as 2026-09-02-12 (explanation letter
and policies), just two categories that fix had not covered.
`evidence_builder.select_evidence_categories()` cites `billing_proof` against
`order:{merchant_order_id}` **unconditionally, for every case** — the order
record is proof of what was billed — but no code rendered a PDF for an order.
Writing a regression test for the first fix (asserting a full case leaves
`draft.unsupported_categories` empty) surfaced a second instance of the same
gap: `customer_communication` is also cited against `order:...` whenever any
communications exist, and had no document behind it either — the demo
scenario only ever looked complete because it happened to also have a
separate chat-transcript file uploaded as a `customer_communication`
document, which masked the gap for every other case that lacks one.

**How I investigated it.** Read the exact warning text the dashboard itself
produced against a real seeded case, rather than re-deriving it from code.
Confirmed with `grep` that `billing_proof` appears exactly once in
`evidence_builder.py`, in the unconditional citation, and nowhere in
`document_generator.py`. Wrote a regression test asserting zero unsupported
categories on a full case before writing the fix — that test caught the
second gap (`customer_communication`) on its first run, before any manual
inspection would have.

**Solution / fix.** Added `generate_billing_proof_pdf()` (the order record as
a billing document) and `generate_communications_transcript_pdf()` (the full
communications log) to `document_generator.py`, mirroring the existing
policy-PDF pattern. Wired both into `contest_service.collect_uploadable_documents()`.

**What I changed in the code.** `src/document_generator.py`: two new
generator functions. `src/contest_service.py`: `collect_uploadable_documents()`
now covers both.

**What I learned.** The Phase 7 fix for policies should have prompted
checking every category evidence_builder cites unconditionally against a
record rather than a document, not just the one that happened to be visible
at the time. Also: a regression test written to prove ONE fix is real found a
SECOND, unrelated instance of the same bug class on its first run — writing
the test as "no category may go unsent" rather than "billing_proof must be
sent" is what made that possible.

**Status.** Resolved and verified — `draft.unsupported_categories` is now
empty on the case that originally showed the warning; full suite 171 passed.
