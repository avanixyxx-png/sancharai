# SancharAI Handbook

**Everything you need to run, change, deploy and fix this project on your own.**

Written for someone who is new to programming. Every command can be copied and
pasted. If something here doesn't match what you see on screen, trust the
screen and update this file.

Last updated: 18 October 2026.

---

## 1. What this project is, in plain words

SancharAI is a website where a business signs up and gets an **AI phone agent**
that makes calls for them, talks to customers, answers questions and books
appointments.

You sell it as a monthly subscription. Your customers connect **their own**
Twilio account, so **they** pay for the phone calls, and **you** charge for the
software.

- **Live website:** https://sanchar.automatehubai.in
- **Code on GitHub:** https://github.com/avanixyxx-png/sancharai (private)
- **Your project folder:** `C:\Users\avani\Downloads\SancharAI-v2`

> ⚠️ **Only this folder is real.** There are older copies on your computer
> (`SancharAI SaaS - Copy`, `voiceai-agent-code-em`, `AI Voice Agent - VoiceIQ
> Edition - Copy`). Ignore them. Never edit them.

---

## 2. The accounts this project uses

| Account | Used for | Where to log in |
|---|---|---|
| **GitHub** (`avanixyxx-png`) | Stores the code | github.com |
| **Render** | Runs the website | dashboard.render.com |
| **Supabase** | The database (all accounts, calls, payments) | supabase.com |
| **Brevo** | Sends emails (signup codes, password reset) | app.brevo.com |
| **Razorpay** | Takes payments from customers | dashboard.razorpay.com |
| **GoDaddy** | Owns the domain `automatehubai.in` and the email inbox | godaddy.com |
| **Twilio** | Phone calls (for *your own* testing) | console.twilio.com |
| **NVIDIA / Groq** | The AI brain | build.nvidia.com / console.groq.com |

**The service name on Render is `sanchar`.**

---

## 3. What each file does

You will mostly only touch the ones marked ✏️.

### The main program

| File | What it does |
|---|---|
| `app.py` | ✏️ **The biggest file.** Every web page and every button. ~65 pages/actions. |
| `models.py` | ✏️ The database: what gets stored (users, calls, settings). |
| `engine.py` | The AI brain: builds what the agent says. |
| `telephony.py` | Talks to Twilio/Plivo: places calls, builds call instructions. |
| `auth.py` | Signup, login, passwords. |
| `security.py` | Encryption, passwords, email links and codes. |
| `plans.py` | ✏️ **Your prices and plan limits.** |
| `mailer.py` | ✏️ The emails you send (wording and design). |
| `booking.py` | Appointment booking and clash checking. |
| `gcal.py` | Google Calendar sync. |
| `scheduler.py` | Runs campaigns in the background, dials contacts. |
| `disposable.py` | Blocks temp-mail signups. |
| `stt.py`, `streaming.py` | Deepgram live speech (⚠️ never tested on a real call). |
| `preflight.py` | Safety checks when starting on the live server. |
| `run.py` | Starts the app (used by Render). |
| `start.py`, `start.bat` | ✏️ Starts the app **on your computer** with ngrok. |

### Pages people see

Everything inside `templates/` is a web page. ✏️ You can edit the words safely.

| File | Page |
|---|---|
| `landing.html` | The homepage people see before signing up |
| `signup.html`, `login.html` | Creating an account, signing in |
| `settings.html` | Where customers paste Twilio and AI keys |
| `billing.html` | Plans and payment |
| `campaigns.html`, `contacts.html`, `agents.html` | The main app |
| `overview.html`, `history.html`, `appointments.html` | Dashboard and records |
| `legal_terms.html`, `legal_privacy.html`, `legal_refund.html` | Legal pages |

### Other folders

| Folder | What's inside |
|---|---|
| `static/` | ✏️ `app.css` controls all colours and design |
| `tests/` | Automatic checks that the app still works |
| `data/` | The temp-mail blocklist |
| `csv_templates/` | Example contact files for customers |

---

## 4. Running the app on your computer

### Start it

**Double-click `start.bat`** in the project folder.

It does three things on its own: stops any old copy, opens an ngrok tunnel so
Twilio can reach your computer, and starts the website on
**http://127.0.0.1:8010**.

### Stop it

Click the black window and press **Ctrl + C**.

### If start.bat doesn't work

Open the folder, type `cmd` in the address bar, press Enter, then:

```
py run.py
```

That starts it at http://127.0.0.1:8000 without ngrok.

### Important: your computer and the live site are separate

| | Your computer | The live site |
|---|---|---|
| Address | `127.0.0.1:8010` | `sanchar.automatehubai.in` |
| Database | A file called `sancharai.db` | Supabase |
| Accounts | Only ones you made locally | Real customers |

An account made on one **does not exist** on the other.

---

## 5. Settings stored outside the code (environment variables)

Passwords and keys are **not** in the code. They live in two places:

- **On your computer:** the file `.env` in the project folder.
- **On the live site:** Render → `sanchar` → **Environment**.

> 🔐 **Never put these in GitHub.** The file `.gitignore` already prevents
> `.env` from being uploaded. Don't change that.

| Name | What it does | Where it must be set |
|---|---|---|
| `DATABASE_URL` | Connects to Supabase. **Without it, data is wiped on every deploy.** | Render only |
| `PUBLIC_BASE_URL` | The site's web address, used in emails and call webhooks | Both |
| `FERNET_KEY` | Encrypts customers' saved keys. **Never change it**, or every saved Twilio key becomes unreadable. | Render |
| `SESSION_SECRET` | Keeps logins valid. Changing it logs everyone out. | Render |
| `BREVO_API_KEY` | Sends emails | Render |
| `MAIL_FROM` | The "from" address: `voice@automatehubai.in` | Render |
| `RAZORPAY_KEY_ID` / `_SECRET` | Payments. `rzp_live_…` is real money; `rzp_test_…` is fake. | Render |
| `SANCHARAI_ENV` | Set to `production` on Render; turns on safety checks | Render |
| `SANCHARAI_SIGNUP_LIMIT` | Max signup codes per hour from one network (default 5) | Optional |
| `SANCHARAI_SKIP_TWILIO_VALIDATION` | **Must be `0` live.** `1` lets anyone fake calls. | Both |
| `GOOGLE_CLIENT_ID` / `_SECRET` | Google Calendar sync (optional) | Render |

---

## 6. Changing something: the full routine

Always in this order:

### Step 1 — Edit the file

Open the file in **Notepad**, **VS Code** or any editor. Save it.

### Step 2 — Test it on your computer

Double-click `start.bat`, open http://127.0.0.1:8010 and check your change.

### Step 3 — Run the automatic checks

Double-click **`run_tests.bat`**. Wait for the end.

The last lines say something like `6 file(s) passed, 0 file(s) failed`.

- **`0 file(s) failed`** → safe to continue.
- **Anything above 0** → your change broke something. It also lists which file
  failed. Undo it (see §9) or fix it.

### Step 4 — Send it to GitHub

In the project folder, type `cmd` in the address bar, press Enter, then run
these three lines one by one:

```
git add -A
git commit -m "describe what you changed"
git push
```

### Step 5 — The live site updates itself

Render notices the push and redeploys, which takes **1–3 minutes**. The site
may show **502 Bad Gateway** for a few seconds. That's normal.

Watch it at Render → `sanchar` → **Events**. Wait for **Live**.

---

## 7. Common changes, with exact places

### Change plan prices or minutes
`plans.py`, near the top. Each plan looks like this:

```python
"starter": {
    "name": "Starter",
    "price_inr": 999,          # ← the price
    "monthly_minutes": 500,    # ← minutes included
```

Current plans: Free 30 min (₹0), Starter 500 min (₹999), Pro 2,500 (₹2,999),
Business 10,000 (₹7,999), Enterprise 50,000 (₹19,999).

### Change words on the homepage
`templates/landing.html`. Edit the text between the tags. Don't delete the
`<` `>` parts.

### Change colours
`static/app.css`, the top of the file:

```css
--primary: #00e5ff;   /* the blue colour */
--green: #00ff9d;
```

### Change the wording of emails
`mailer.py`. Look for `send_signup_code`, `send_password_reset`,
`send_invite`.

### Change how long a signup code lasts
`security.py`: `SIGNUP_CODE_TTL = 600` (that's seconds, so 10 minutes).

### Block another temp-mail domain
`disposable.py`: add it inside `_EXTRA = {"dreameg.com"}`.

### Add another AI model to the dropdown
`templates/settings.html`, find `<datalist id="nvidia-models">` and add a line.

### Change the agent's voice or greeting
**Don't edit code.** These are in **Settings** on the website, per customer.

---

## 8. The automatic tests

Double-click **`run_tests.bat`**. Each file checks one area:

| Test | What it proves |
|---|---|
| `test_webhook_signature.py` | Real calls are answered, fake ones rejected |
| `test_signup_verify.py` | Signup codes, temp-mail blocking |
| `test_razorpay.py` | Payments, and that a payment can't be reused |
| `test_nvidia.py` | AI model settings are correct |
| `test_streaming.py`, `test_stt.py` | Deepgram live speech |

Tests use their own throwaway database. **They never touch real customer data.**

> ⚠️ Earlier tests for booking, calendar, agents and language switching were
> lost when a temporary folder was cleared. Those features have no automatic
> checks right now. Test them by hand after changing them.

---

## 9. Deploying and undoing

### Normal deploy
Just `git push` (see §6). Render does the rest.

### Force a deploy
Render → `sanchar` → **Manual Deploy** → **Deploy latest commit**.

### Undo your last change (before pushing)
```
git checkout -- .
```

### Undo a change that's already live
```
git revert HEAD
git push
```
This makes a new commit that reverses the last one, then deploys it.

### Go back to an older version in Render
Render → `sanchar` → **Events** → find an older successful deploy →
**Rollback**.

---

## 10. When something breaks

| What you see | What it means | What to do |
|---|---|---|
| **502 Bad Gateway** | Render is restarting | Wait 2 minutes, refresh |
| **Site slow on first visit** | Free plan sleeps after 15 min idle | Upgrade to Starter (~$7/mo), or set up UptimeRobot to ping `/health` every 5 min |
| **Call rings then cuts instantly** | The app rejected Twilio's request | Render → Logs, search `signature`. Check `SANCHARAI_SKIP_TWILIO_VALIDATION` is `0` and `PUBLIC_BASE_URL` matches the real domain |
| **"An application error has occurred"** | The AI took too long or crashed | Settings → AI brain → use a fast model (Groq `llama-3.1-8b-instant`) |
| **Signup email never arrives** | Brevo problem | Brevo → Transactional → Logs. Also check spam. Render → Logs, search `signup code` |
| **All accounts disappeared** | `DATABASE_URL` missing on Render | Add it back immediately; the data is still in Supabase |
| **Test call fails: no credit** | Twilio balance is empty | console.twilio.com → Add funds |
| **Local site won't start** | Old copy still running | Close the black window, double-click `start.bat` again |
| **Twilio can't reach your computer** | ngrok restarted with a new address | `start.bat` fixes this automatically; just restart it |

**Where to find errors:**
- **Live site:** Render → `sanchar` → **Logs**
- **Your computer:** the black window `start.bat` opened

---

## 11. Who pays for what

**You pay (monthly):**
- Render hosting: $0 now (free plan), ~$7 if you upgrade
- Supabase, Brevo: free tiers
- Your own Twilio testing

**Your customer pays:**
- Your plan (₹999–₹19,999/month) through Razorpay
- **Their own Twilio bill**, about ₹4.35/min to an Indian mobile from a US
  number, plus ~₹10/min Twilio speech charges
- Their own AI key (Groq and NVIDIA have free tiers)

**The uncomfortable truth:** a Starter customer pays you ₹999 but may pay
Twilio ₹7,500 for 500 minutes. Cutting that bill needs either Deepgram
streaming (built, untested) or an Indian phone number (needs KYC documents).

---

## 12. Security rules

1. **Never** put passwords or keys in the code or in GitHub.
2. **Never** remove `.env`, `secret.key` or `*.db` from `.gitignore`.
3. **Never** share a key in a chat or screenshot. If you do, replace it.
4. **Never** change `FERNET_KEY` on Render. Every saved customer key dies.
5. Keep `SANCHARAI_SKIP_TWILIO_VALIDATION=0` on the live site.

**Still to rotate** (these were shown in chat during development):
- Supabase database password → then update `DATABASE_URL` in Render
- Brevo API key → then update `BREVO_API_KEY` in Render

---

## 13. What's finished and what isn't

### Works
Signup with email code, temp-mail blocking, login, password reset, Twilio
calling, AI replies, 7 languages, appointment booking, Google Calendar sync,
human handoff, campaigns, contacts CSV, call history, Razorpay payments,
team invites, admin page.

### Built but never tested on a real call
- **Deepgram live speech** (`stt_provider = deepgram`). Leave it off.
- **Plivo** as a phone provider. Needs Indian KYC documents.

### Not done yet
- **UptimeRobot** ping, so the free site doesn't sleep
- **Razorpay webhook**: if a customer closes the tab right after paying, their
  plan may not upgrade
- **Rebuilding the lost tests**
- **Indian phone number** (needs GST or business registration)

---

## 14. Getting help from ChatGPT or another AI

Paste this first, then your question:

```
I have a Python web app called SancharAI. Facts about it:

- FastAPI + SQLModel + Jinja2 templates. No React, no build step.
- Database: Supabase Postgres live (DATABASE_URL), SQLite locally.
- Hosted on Render, service "sanchar", auto-deploys from GitHub main branch.
- app.py has all the web routes. models.py is the database.
  templates/ holds the HTML pages. tests/ holds the tests.
- Runs locally with start.bat on port 8010.
- I am a beginner. Give me exact copy-paste steps and tell me
  which file and which line to change.

My question: <write your question here>
```

**Then paste the file you're asking about**, or the error message exactly as it
appears.

**Good questions:** "Here is my plans.py, how do I add a plan called Lite at
₹499 for 200 minutes?"

**Risky:** "Rewrite my app." Never accept a change you can't test with
`run_tests.bat`.

---

## 15. Word list

| Word | Meaning |
|---|---|
| **Deploy** | Put your new code on the live website |
| **Commit** | Save a snapshot of your changes |
| **Push** | Upload commits to GitHub |
| **Repo** | The project's storage on GitHub |
| **Environment variable** | A setting kept outside the code, like a password |
| **Webhook** | Twilio calling your app to ask what the agent should say |
| **ngrok** | Gives your computer a temporary public address so Twilio can reach it |
| **Migration** | Adding a new column to the database |
| **STT / TTS** | Speech to text / text to speech |
| **KYC** | Identity documents the government requires for phone numbers |
| **502** | The server is restarting or crashed |

---

## 16. If you only remember five things

1. Work in **`SancharAI-v2`**. Ignore the other folders.
2. Test on your computer with **`start.bat`** before touching the live site.
3. Run **`run_tests.bat`** before every push.
4. Push with **`git add -A`**, **`git commit -m "..."`**, **`git push`**. Render does the rest.
5. When it breaks, read **Render → Logs**. The answer is almost always there.
