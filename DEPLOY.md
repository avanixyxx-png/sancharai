# Going live

Everything here is something that is fine locally and harmful in production.
The app enforces most of it at startup — set `SANCHARAI_ENV=production` and it
refuses to boot on unsafe config rather than failing quietly later.

---

## 1. Generate your keys

Run these **once** and keep the output somewhere safe:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The first is `FERNET_KEY`, the second `SESSION_SECRET`.

> **Do not reuse `secret.key` from your laptop, and do not lose these.**
> `FERNET_KEY` decrypts every customer's stored Twilio and AI credentials.
> Change it and those credentials become permanently unreadable — calls stop
> working with no obvious cause, and there is no recovery.

## 2. Push to GitHub

`.gitignore` already excludes `secret.key`, `.env`, the database and logs.
Check before pushing anyway:

```bash
git init
git add .
git status          # secret.key / .env / *.db must NOT appear
git commit -m "SancharAI"
git remote add origin https://github.com/YOU/sancharai.git
git push -u origin main
```

## 3. Environment variables

| Variable | Value | If you skip it |
|---|---|---|
| `SANCHARAI_ENV` | `production` | Unsafe settings pass silently |
| `FERNET_KEY` | from step 1 | **Credentials orphaned on every deploy** |
| `SESSION_SECRET` | from step 1 | Everyone logged out on every deploy |
| `PUBLIC_BASE_URL` | `https://yourdomain.com` | **Every call fails** with "application error" |
| `PLATFORM_ADMINS` | your email | `/admin` falls back to the developer's address |
| `SANCHARAI_DB` | `/data/sancharai.db` | Database wiped on every deploy |
| `BREVO_API_KEY` | from brevo.com | Nobody can verify their email or reset a password |
| `RAZORPAY_KEY_ID` / `_SECRET` | from razorpay.com | Nobody can pay you |

Leave `SANCHARAI_ALLOW_DEMO_UPGRADES`, `SANCHARAI_SKIP_TWILIO_VALIDATION` and
`SANCHARAI_SECURE_COOKIES=0` **unset**. The preflight check refuses to start if
any of them are on in production.

## 4. Hosting requirements

Your app needs three things that rule out most free tiers:

- **A persistent disk** — SQLite is a file. Ephemeral filesystems lose it.
- **An always-on process** — the scheduler dials every 15s; scale-to-zero stops campaigns.
- **Exactly one instance** — two schedulers dial every contact twice.

`render.yaml` in this repo is preconfigured correctly. If you use multiple web
workers later, set `SANCHARAI_SCHEDULER=0` on all but one.

## 5. After the first deploy

1. Sign up, and **verify the email actually arrives** — that proves Brevo works
2. Settings → connect Twilio, add your AI key, pick a voice
3. **Test Call** to your own phone — proves the whole webhook chain works
4. Check `/admin` loads for you and 404s for a normal account

## 6. Backups

`sancharai.db` holds every call transcript and all encrypted credentials.
Copy it somewhere off the server nightly:

```bash
sqlite3 /data/sancharai.db ".backup '/tmp/backup.db'"
```

A file copy of a live SQLite database can be corrupt; `.backup` is safe.

---

## Known limits

**SQLite** is fine into the low thousands of customers. Move to Postgres when
you need multiple app servers — it is a one-line change in `models.py`.

**Chirp3 voices are Public Beta** at Twilio. The single-language Polly voices
remain available as a fallback if a beta voice misbehaves.

**The legal pages are drafts.** `/legal/terms`, `/legal/privacy` and
`/legal/refund` describe accurately what the software does, but have not been
reviewed by a lawyer. Do that before taking real payments.

**Call recordings are not stored** — only text transcripts. Enabling recording
brings consent and disclosure obligations that transcripts alone do not.
