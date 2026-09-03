# AI Chargeback Defense Agent

**Razorpay AI Buildathon — Track 2: AI Risk Manager**

![pipeline](docs/pipeline.svg)

## Hi, I'm Abiram

I picked Track 2 — stop merchants bleeding money to fraud, returns and
chargebacks — because the numbers on the ground in India don't match the
textbook chargeback story:

- **UPI is ~85% of India's digital payment volume now** (NPCI, FY 2025-26,
  554M+ users, 65M+ merchants). Most disputes I'd actually see aren't "stolen
  card" — they're a UPI app showing *failed* while the money genuinely left.
- **Indian D2C sellers run RTO rates of 25–35%**, against an 8–12% global
  benchmark, at roughly ₹150–300 lost per returned order. That's a bigger
  drain than card fraud for most merchants here.
- Globally, every **$1** a merchant loses to a chargeback costs them **$5.13**
  all-in once you count fees, lost goods and admin time (LexisNexis, *True
  Cost of Fraud* 2026) — and ~74% of disputes that get raised go all the way
  to a full chargeback rather than getting resolved earlier.

So the problem isn't "detect fraud" — Razorpay already does that. The problem
is: once a dispute exists, someone has to dig through order systems, courier
tracking and support chats, decide fast (there's a hard deadline), and get it
right in both directions — don't contest what you'll lose, don't give up on
what you'd win. That's slow, manual, and easy to get wrong under pressure.
That's what I built this to fix.

## How I'm solving it

```
Razorpay dispute → AI reads the merchant's own records → verdict + evidence
   → human approves/rejects → draft → human submits → Razorpay → bank
```

The AI never acts alone. It reads real records (never invents anything —
every claim it makes has to point at an actual row in the database or it
gets rejected), and hands a human a recommendation, not a decision. Nothing
gets drafted, and nothing gets submitted, without someone clicking to make
it happen.

Razorpay gives no one — not me, not any merchant — a way to spin up a test
dispute, so the demo dispute events are simulated to match Razorpay's real
dispute schema exactly, clearly labelled as such everywhere they appear.
Full story on that, and everything else, in [ARCHITECTURE.md](ARCHITECTURE.md).

## What I built

| Phase | What | Status |
|---|---|---|
| 1 | Real Razorpay Test Mode integration | ✅ |
| 2 | Dispute ingestion — real webhook + labelled simulator | ✅ |
| 3 | Merchant database + 9 realistic dispute scenarios | ✅ |
| 4 | AI investigator — Groq, every citation checked against real data | ✅ |
| 5 | Evidence builder — PDFs, Razorpay's evidence categories | ✅ |
| 6 | Human review dashboard | ✅ |
| 7 | Contest draft → human-gated submit to Razorpay | ✅ |
| 8 | Held-out evaluation — precision/recall/F1, $ impact | next |
| 9 | Failure-mode demo | pending |
| 10 | Polish | pending |

183 tests, all passing. Every real bug I hit along the way — and a few I
almost shipped — is written up in [BUILD_LOG.md](BUILD_LOG.md), not swept
under the rug.

## See it running

Screenshots aren't in this repo yet — I don't have a working headless
browser in my build environment (missing system libs, needs `sudo` I don't
have here). Easiest fix, one line:

```bash
sudo apt-get install -y libnspr4 libnss3 libasound2t64
```

Until then, the dashboard's a 30-second `streamlit run` away — see below.

## Setup

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` — four values, nothing else:

| Variable | Where to get it |
|---|---|
| `RAZORPAY_KEY_ID` | Razorpay Dashboard → Test Mode → Settings → API Keys |
| `RAZORPAY_KEY_SECRET` | shown once when you generate the key |
| `RAZORPAY_WEBHOOK_SECRET` | the secret on your webhook endpoint |
| `GROQ_API_KEY` | console.groq.com/keys |

## Run it

```bash
uv run scripts/seed_merchant_db.py --reset   # 9 demo cases, AI-investigated
uv run streamlit run dashboard/app.py         # http://localhost:8501
```

That's it. Every case is already investigated when the dashboard opens —
just review and approve/reject.

## Read more

- [ARCHITECTURE.md](ARCHITECTURE.md) — full technical writeup, phase by phase
- [BUILD_LOG.md](BUILD_LOG.md) — real problems I hit and how I fixed them
- [NOTES.md](NOTES.md) — the detailed engineering log
