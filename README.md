# SancharAI

Multi-tenant AI voice calling platform. Each customer connects their **own**
Twilio account and **own** Groq API key, so telephony and AI costs go directly
to them and you bill only for agent minutes.

This is a merge of two earlier codebases:

| | Taken from |
|---|---|
| Backend, database, call engine, encryption, multi-tenancy | **SancharAI v1** |
| Agents, contacts, plans, scheduler, team invites | **voiceai-agent (v2)** |
| Design language, landing page, multi-provider AI, lead scoring, CSV export, cost controls | **VoiceIQ edition** |

No MongoDB. No Node build step. SQLite + server-rendered Jinja, so `python run.py`
is the whole story.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
python run.py               # http://127.0.0.1:8000
```

Sign up, then open **Settings** and add:

1. **Twilio** — Account SID, Auth Token, and your Twilio phone number
2. **An AI brain** — pick one (see below)
3. **Workspace timezone** — call windows and daily caps are evaluated in this zone

### Choosing an AI provider

Each company picks its own in Settings, and "Test AI connection" round-trips it
so a bad key or unreachable host surfaces there rather than mid-call.

| Provider | Cost | Notes |
|---|---|---|
| **Groq** (default) | Free tier | ~0.5s replies. Best default for live calls. Key from [console.groq.com](https://console.groq.com). |
| **Ollama** | Free | Runs on **your own** server, so no API key and no per-call cost — and the conversation never leaves your network. Needs `ollama pull llama3.2` first. |
| **OpenAI** | Paid | Billed to your own OpenAI account. |

A note on Ollama: the host must be reachable *from wherever SancharAI runs*. If
SancharAI is deployed and Ollama is on your laptop, `localhost` will not work.
Local models are also slower than Groq, and on a phone call any pause over about
three seconds is dead air the customer hears.

To place real calls Twilio must be able to reach you, so set `PUBLIC_BASE_URL`
in `.env`. Locally:

```bash
ngrok http 8000
# then put the https URL in PUBLIC_BASE_URL and restart
```

Verify your email (the link is emailed, or printed to the server log when
`BREVO_API_KEY` is unset) — calls stay blocked until you do.

---

## How calling works

1. Create an **Agent** (reusable persona: voice, language, personality)
2. Create a **Campaign** (goal, call window, daily cap, retry policy)
3. Upload a **CSV** of contacts under *Contacts & CSV* — 27 ready-made
   industry templates are included
4. Press **Start**

The background scheduler then dials contacts within the call window, in your
workspace timezone, respecting the daily cap and the plan's minute allowance.
Every call is transcribed, classified (interested / confirmed / callback / …),
summarised, and stored.

Any CSV column beyond `phone` becomes a `{placeholder}` usable in the agent's
script — upload `phone,name,product` and write *"Hi {name}, about your {product}…"*.

---

## Architecture

```
app.py         routes: pages, JSON actions, Twilio webhooks
models.py      SQLModel tables + in-place column migrations
engine.py      call engine: CSV parsing, Twilio, Groq, transcripts, outcomes
scheduler.py   background dialer: call windows, daily caps, retries
plans.py       subscription tiers + enforcement + payment verification
auth.py        signup, login, team invites, session guard
security.py    password hashing, credential encryption, sessions, CSRF
mailer.py      transactional email via Brevo
```

**Tenancy.** Every table carries `company_id`, and `get_current_user()` is the
only way a request learns which company it belongs to — no route accepts a
company id from the client.

**Timestamps.** Everything is stored as naive UTC via `utcnow()` and converted
to the company's timezone only at the edges.

**Live call state.** Conversation history lives in the `callsession` table, not
in memory, so a restart mid-call doesn't lose context.

---

## What was fixed in the merge

**Security**

- Twilio webhook signature validation — neither version verified that callbacks
  actually came from Twilio, so anyone who saw a webhook URL could drive the agent
- CSRF protection on every mutating route — v1 had none, so any site could submit
  a form that deleted an account
- `secret.key` is no longer committed; keys come from `FERNET_KEY` /
  `SESSION_SECRET`, with the file only as a local fallback (see `.gitignore`)
- Session cookies are `Secure` by default
- **Billing.** v2's upgrade endpoint granted any plan for free with a fake
  "paid_demo" invoice. Upgrades now require a verified Razorpay signature, or an
  explicitly enabled and loudly logged demo grant
- Account deletion removes every company-scoped table — v1 orphaned FAQ rows
- CSV template downloads are path-traversal safe

**Correctness**

- **Call windows use the company's timezone.** v2 compared against UTC, so
  `09:00–20:00` set by an Indian user actually ran `14:30–01:30` IST — it rang
  people at 1:30am
- Daily caps reset at the company's local midnight, not UTC's
- `retry_policy` is finally honoured; v1 stored the field and never read it
- Conversation history is persisted, so a restart mid-call no longer loses the
  customer's name and context, and the agent remembers earlier turns (v2 sent
  only the newest sentence, so it had no memory at all)
- Call duration comes from Twilio's `CallDuration` instead of an in-memory timer
- Answering-machine detection hangs up instead of billing a full voicemail

**Performance**

- Calls are dialed by a background worker. v1 looped over the whole list inside
  the HTTP handler, freezing every tenant's dashboard until it finished
- Blocking Twilio and Groq calls run in a threadpool, off the event loop
- Dashboard stats are SQL aggregates rather than loading every call row into memory

**Operability**

- A pinned `requirements.txt` — neither version shipped one that worked
- Automatic column migrations, so an existing database keeps its data
- A scheduler lock file, because two workers would dial every contact twice

---

## Deployment notes

- **Run exactly one scheduler.** Set `SANCHARAI_SCHEDULER=0` on every process
  except one. A lock file guards against mistakes, but don't rely on it.
- **Set `FERNET_KEY` and `SESSION_SECRET`** as real environment variables.
  Losing `FERNET_KEY` makes every stored credential permanently undecryptable.
- **`PUBLIC_BASE_URL` must be set** — the scheduler has no incoming request to
  infer it from, and campaigns pause with a visible reason if it's missing.
- **Never set `SANCHARAI_ALLOW_DEMO_UPGRADES=1` in production.**
- SQLite is fine into the low thousands of calls. Past that, point
  `models.engine` at Postgres — no other code changes are needed.

---

## Configuration

All settings live in `.env` — see `.env.example` for the annotated list.
Only `PUBLIC_BASE_URL` is required before real calls will work; email, payments,
and admin access degrade gracefully when unconfigured.
