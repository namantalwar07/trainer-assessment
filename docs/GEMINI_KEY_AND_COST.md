# Gemini API: getting a key and what it costs (for approval)

Facts below were read from Google's own pages on 6 Nov 2026 (links at the end). Figures marked *estimate* are
ours and must be replaced by measured numbers after the first pilot.

## 1. Getting an API key (about 15 minutes)

1. **Use a company-owned Google account**, not a personal one, so access survives staff changes.
2. Open <https://aistudio.google.com/app/apikey> and choose **Create API key**. Pick or create a Google Cloud
   project (for example "trainer-assessment").
3. **Switch the project to the paid plan**, because the free tier is not allowed for real recordings (see 4):
   API keys page, or Settings > Plan information, then **Set up billing** next to the project.
   - Choose your country, accept the terms, confirm contact details and add a payment method.
   - Choose **Prepay** or **Postpay**. Prepay needs a minimum of **$5** (maximum $5,000); credits expire after
     12 months and are non-refundable. When the balance hits $0 the API stops with HTTP 402. Auto-reload is optional.
   - Linking an active billing account makes the project **Tier 1** (default monthly spend limit $250).
4. **Set a monthly spend cap** on the Spend page (Monthly spend cap > Edit; experimental feature) as a safety net.
5. Copy the key into `.env` as `GEMINI_API_KEY=...`, run `make doctor`. Never paste the key in chat, email or code.
   If it leaks, delete it in AI Studio and create a new one.

Not stated on Google's pages (ask the manager to confirm before relying on it): whether an Indian company card is
accepted, and whether GST is added to invoices. Check that the card allows international online payments.

## 2. What it costs (model: gemini-3.8-flash, paid Standard tier)

| Item | Until 31 Dec 2026 | From 1 Jan 2027 |
|---|---|---|
| Input, per 1M tokens | $0.75 | $1.50 |
| Output incl. thinking, per 1M tokens | $3.75 | $7.50 |

Video is billed as input tokens: about **100 tokens per second** at the default (low) media resolution, about
**300 per second** at high resolution, plus 32 per second of audio is already counted in these. A 75-minute
recording is therefore about 450,000 input tokens at low resolution.

Estimate per 75-minute recording (two steps: one video pass, then a cheap text-only scoring pass), at Rs 95 per $:

| Mode | Per recording | 30-video pilot | All 450 recordings | If run after 1 Jan 2027 |
|---|---|---|---|---|
| Default (low) resolution | about Rs 47 | about Rs 1,400 | **about Rs 21,000** | about Rs 43,000 |
| High resolution (clearer on-screen clock/text) | about Rs 112 | about Rs 3,300 | **about Rs 50,000** | about Rs 100,000 |
| Agentic mode (Google: "up to 88% fewer tokens") | not yet known | measure in pilot | likely at or below the low figure | double |

Add about Rs 1,500 to Rs 3,000 a month for a small server if hosted (nothing extra if run on an office PC).
Taxes may apply on top.

**Why the resolution question matters:** reading the meeting clock and on-screen names needs legible frames. The
pilot decides whether low resolution is enough; if not, the cost is about 2.4 times higher.

**Timing:** prices double on 1 Jan 2027. Finishing the full set before 31 Dec 2026 saves roughly Rs 20,000 to
Rs 50,000. Practical plan: pilot of 30 recordings this week, then the rest in batches of 10 to 20.

**Suggested first prepay:** $50 (about Rs 4,750) covers the pilot and a first batch; top up as needed.

## 3. Data protection points for the same conversation

- Paid-tier content is **not** used to improve Google's products; free-tier content **is**. Use the paid plan only.
- Recordings contain identifiable people and mentee names: get consent and write down who can access what and how
  long files are kept (India DPDP Act 2023). The app deletes the uploaded copy at Google after each job.

## Sources

- Pricing: <https://aistudio.google.com/docs/pricing> (redirected from ai.google.dev/gemini-api/docs/pricing)
- Video token rates and agentic mode: <https://aistudio.google.com/docs/video-understanding>
- Billing, prepay, tiers, spend cap: <https://aistudio.google.com/docs/billing>
