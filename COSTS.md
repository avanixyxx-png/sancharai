# What SancharAI uses, and what it costs

Every service the product depends on, what it's for, and its real price.

Prices marked ✅ were read from the provider's own pricing page during
development. Prices marked ⚠️ are estimates or couldn't be verified — treat
those as rough. Dollar prices use **₹88 = $1**.

Last checked: 21 November 2026. Re-check before making a pricing decision.

---

## 1. The short answer: who pays for what

| | Pays for | Roughly |
|---|---|---|
| **You** | Hosting the website, emails, the domain | **₹0–700/month** |
| **Your customer** | Their own phone calls (Twilio) and AI key | ₹2,600–7,500 per 500 minutes |
| **Your customer** | Your subscription | ₹999–19,999/month |

**You currently pay nothing per minute of calling.** Customers connect their
own Twilio account, so call charges go straight to them. That's the single
most important fact about your cost structure.

---

## 2. Your monthly running costs

| Service | What it does | Current cost | If you grow |
|---|---|---|---|
| **Render** | Runs the website | **₹0** (free plan) | ~₹615/mo ($7) for the Starter plan ✅ |
| **Supabase** | The database | **₹0** (free tier) | Free up to 500 MB ✅ |
| **Brevo** | Signup and reset emails | **₹0** (300/day free) ✅ | Paid above that |
| **GoDaddy** | Domain + info@ inbox | Already paid ⚠️ | Renews yearly |
| **Razorpay** | Takes payments | No monthly fee | A % of each payment ⚠️ not verified |
| **Twilio** | Your own test calls | Pay as you go | See below |

**Total today: about ₹0/month**, plus whatever you spend testing.

**Two things worth knowing about the free plans:**
- **Render free sleeps** after 15 minutes idle. The next visitor waits ~17
  seconds, and a call arriving then fails. Fixed by a free uptime pinger or
  the ₹615/mo plan.
- **Supabase free has no backups** and pauses after a week of no activity.

---

## 3. What one minute of conversation costs

A call has four parts. Only the first is unavoidable.

| Part | What it is | Cost/min |
|---|---|---|
| **The call** | Connecting to the phone | ₹4.35 (Twilio) or ₹0.38 (Indian number) ✅ |
| **Listening** | Turning speech into text | ₹5.25 (Twilio) or ₹0.51 (Deepgram) ✅ |
| **Speaking** | Turning text into voice | ₹0.35 (standard) or ₹5.70 (Chirp3-HD) ✅ |
| **Thinking** | The AI writing the reply | ₹0 (free tiers) |

### The combinations

| Setup | Cost/min | Needs |
|---|---|---|
| **Today: Twilio + Chirp3-HD voice** | **₹15.00** | — |
| Twilio + standard voice | ₹10.00 | One dropdown change |
| **Twilio + Deepgram listening** | **₹5.20** | Free Deepgram key + one test call |
| Indian number + Deepgram | ₹0.89 | GST/business registration |

**₹15 → ₹5.20 needs no paperwork.** Two thirds of the saving comes from
moving the listening off Twilio.

---

## 4. Why Twilio is expensive: it bills per turn

Twilio charges **₹1.75 every time the caller speaks**, not per minute ✅.

| Caller | Turns/min | Twilio listening cost | Deepgram |
|---|---|---|---|
| Quiet | 2 | ₹3.50 | ₹0.51 |
| Normal | 3 | ₹5.25 | ₹0.51 |
| Chatty | 6 | ₹10.50 | ₹0.51 |

You can't control how talkative someone is, so per-turn billing is a cost you
carry blindly. Deepgram's flat per-minute rate removes that risk.

---

## 5. Every service, one by one

### Twilio — phone calls
- **India mobile, outbound:** $0.0496/min = **₹4.35** ✅
- **Speech recognition:** $0.02 per turn = **₹1.75** ✅
- **Text-to-speech:** standard $0.0008/100 chars (₹0.35/min), neural
  $0.0032/100 (₹1.41/min), generative/Chirp3-HD $0.0130/100 (**₹5.70/min**) ✅
- **Phone number:** ~$1–2/month ⚠️
- **No monthly fee** — pay as you go ✅
- **$20 ≈ 117 minutes** at today's settings
- **Trial accounts** can only call numbers you've verified, and play a trial
  message first

### Plivo — cheaper Indian calls (set up in code, unused)
- **India domestic:** **₹0.38/min** ✅
- **Speech recognition:** ₹1.70 per 15 seconds ✅
- **Text-to-speech:** **free** ✅
- **Blocked:** needs Indian KYC documents (GST, Udyam or similar)

### Deepgram — cheaper listening
- **Nova-3 multilingual streaming:** $0.0058/min = **₹0.51** ✅
- **Nova-3 English only:** $0.0048/min = ₹0.42 ✅
- **$200 free credit ≈ 43,000 minutes**, no card, no expiry ✅
- **Aura text-to-speech:** Aura-1 $0.015/1k chars (₹0.66/min), Aura-2
  $0.030/1k (₹1.32/min) ✅ — but **no Indian languages** ✅, so unusable for
  Hindi or Gujarati
- **Status in your app:** built, never tested on a real call

### Every listening option compared ✅ (checked 21 Nov 2026)

| Provider | Price | ₹/min | Live streaming? | In your app |
|---|---|---|---|---|
| Twilio (current) | ₹1.75/turn | **₹5.25** | ✅ | ✅ |
| **Deepgram Nova-3 multilingual** | $0.0058/min | **₹0.51** | ✅ | ✅ built |
| Deepgram Nova-3 English | $0.0048/min | ₹0.42 | ✅ | ✅ built |
| AssemblyAI real-time | $0.30/hr | ₹0.44 | ✅ | ❌ needs code |
| AssemblyAI voice-agent tier | $0.45/hr | ₹0.66 | ✅ | ❌ |
| **Groq Whisper turbo** | $0.04/**hour** | **₹0.06** | ❌ files only | ❌ needs code |

Groq is by far the cheapest but cannot stream: using it on a live call means
writing the audio chunking and silence detection yourself, which is most of
what Deepgram is sold for.

### Every speaking option compared ✅

| Provider | Price | ₹/min at ~500 chars | Indian languages? |
|---|---|---|---|
| **Twilio standard** | $0.0008/100 chars | **₹0.35** | ✅ yes |
| Deepgram Aura-1 | $0.015/1k chars | ₹0.66 | ❌ no Hindi |
| Twilio neural | $0.0032/100 chars | ₹1.41 | ✅ |
| Twilio Chirp3-HD | $0.0130/100 chars | ₹5.70 | ✅ best, all 7 |
| ElevenLabs | credit-based | ~₹4–8 ⚠️ | ⚠️ not stated |

**Conclusion: Twilio standard voice is the cheapest verified option that
speaks Indian languages.** Deepgram's own voice is cheaper than Chirp3-HD but
cannot speak Hindi, so it is not an option here.

### The AI brain
- **Groq:** free tier, best for Indian languages
- **NVIDIA:** free endpoints. Default model
  `nemotron-3.5-lightning-30b-a3b`; its official languages **exclude Hindi
  and Gujarati** ✅
- **OpenAI:** paid, billed to the customer's own key
- **Cost to you: ₹0.** Customers bring their own key.

### Supabase, Brevo, Render, Razorpay
- **Supabase free:** 500 MB, pauses after 1 week idle, **no backups** ✅
- **Neon free** (the alternative we didn't pick): 0.5 GB, permanent, 1 snapshot ✅
- **Brevo free:** 300 emails/day ✅
- **Render:** free sleeps and has no disk; Starter ~$7/mo; disks $0.25/GB/mo ✅
- **Razorpay:** no monthly fee; per-transaction % ⚠️ not verified — check your
  dashboard

### Compared with buying a ready-made platform
- **Vapi:** $0.05/min platform fee (₹4.40) **on top of** telephony and AI ✅
- **Haptik (Jio):** no public pricing, enterprise sales only ✅

---

## 6. Real scenarios

### Testing (you, now)
| | Cost |
|---|---|
| Twilio top-up | ₹1,760 ($20) → ~117 minutes |
| Everything else | ₹0 |

### One Starter customer using 500 minutes/month
| | Today | After Deepgram |
|---|---|---|
| Their Twilio bill | ₹7,500 | ₹2,600 |
| They pay you | ₹999 | ₹999 |
| **Your cost** | **₹0** | **₹0** |

Their bill dwarfs your fee either way — which is exactly why cutting it makes
your product easier to sell.

### If you ever supplied the calls yourself ("ready-made")
At ₹5.20/min cost against ₹2/min of revenue on Starter, 500 minutes would
cost you ₹2,600 and earn ₹999. **You'd lose money on every customer.**
Ready-made only works at the ₹0.89/min Indian-number rate.

---

## 7. How to cut costs, in order

| # | Action | Saves | Effort |
|---|---|---|---|
| 1 | Switch voice from Chirp3-HD to a standard voice | ₹15 → ₹10/min | One dropdown ✅ free |
| 2 | Turn on Deepgram listening | → ₹5.20/min | Free key + one test call |
| 3 | Keep calls short (the agent already replies in one sentence) | Proportional | Done |
| 4 | Hang up on silent calls | ~₹40 per dead call | Done ✅ |
| 5 | Voicemail detection | Avoids whole wasted calls | On by default |
| 6 | Indian number (Plivo) | → ₹0.89/min | Needs KYC |
| 7 | Self-hosted speech models | ~₹0.1/min | Only above ~9,000 min/month |

---

## 8. What isn't verified

Be careful quoting these:
- **Razorpay's transaction fee** — check your dashboard
- **Twilio phone number monthly rental** — check Console → Phone Numbers
- **Plivo's exact KYC document list** — their pages wouldn't load; ask support
- **Google Cloud speech prices** — both pricing pages block automated reading
- **OpenAI API prices** — their public page shows only ChatGPT seat pricing
- **Telnyx, Sinch, Vonage rates to India** — none publish one; Telnyx and
  Sinch keep theirs in a downloadable price sheet, Vonage blocks the page
- **ElevenLabs per-minute cost and Hindi support** — credit-based, not stated
- **The ₹88 = $1 rate** — changes daily

---

## 9. Re-checking these numbers

| Service | Where |
|---|---|
| Twilio | twilio.com/en-us/voice/pricing/in |
| Plivo | plivo.com/voice/pricing/in/ |
| Deepgram | deepgram.com/pricing |
| Render | render.com/pricing, and your own dashboard |
| Razorpay | your dashboard → Pricing |

Prices change. Treat anything here older than a few months as a hint, not a
fact.

---

## 10. The one-line summary

**Your costs are near zero. Your customer's Twilio bill is the expensive
part, and it can be cut by two thirds with a free Deepgram key and one test
call.** Everything else is optimisation.
