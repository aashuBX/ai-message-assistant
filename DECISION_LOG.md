# Decision log

**Shape: the model reads, code decides.** An LLM labels each message: intents with verbatim evidence, entities and risk flags. A policy table in code picks the route, queue and priority. The model has no tools and no authority, so a successful injection can at worst mislabel. Money, health, safety and attacks go to a person whatever the model says. Automation only takes read-only or reversible actions; that bounds the cost of a wrong call more than any threshold does.

**What I noticed, and what I did**
- **Several requests in one message.** Four messages hold 2–3, sometimes for different teams (008: refund → support, new order → sales). Intents are a list with a handler each, and each message has one owner.
- **Not a customer wanting something.** Eight messages: an injection (005), a vendor invoice with the bank-details-in-attachment fraud pattern (021), a job application (024), a B2B lead (013), praise (010, 023), nothing actionable (007, 012). Sender type, "unclear" and "no action" are real outcomes.
- **Clean bytes, messy meaning.** MSG-025's `null` is likely a WhatsApp voice note or photo, so a person looks rather than the system guessing.
- **No sender, thread or attachment fields.** Nothing can check who owns VW-48812 or what "the other one" is. I state this as an assumption; context-dependent messages go to people.
- **"Unsubscribe" alone (017).** It does what is safe under both readings (marketing opt-out) and never cancels a paid subscription on that word.
- **Timezones.** Every timestamp is IST, but Voyage's customers are in Toronto: the stranded family in 016 wrote at about 4:20 am local. P1 needs 24/7 cover.

**Chose not to build**
- **Replies to customers.** Written replies are where model errors become harm (health advice, payment details).
- **Integrations** (orders, bookings, CRM, helpdesk). The output is a dispatch decision; nothing executes.
- **A queue or HTTP service.** 10,000 a day is about 7 a minute; `triage_records()` is stateless and fits any worker.
- **Attachments, voice notes, thread history.** They aren't in the input.
- **A trained classifier.** There's no labelled data; my 25 labels are an eval set.
- **A paid LLM provider.** This is a practice project, so the model (`openai/gpt-oss-120b`) runs on a free Groq key; production would need a paid plan.

**Where this breaks**
- **Confidence is a heuristic, not a calibrated probability.** The 25 labels are mine, and I wrote the prompt against those messages, so scores on them are optimistic.
- **The weakest-request rule is conservative.** One hedged request ("thinking of booking") sends an automatable message to a person: safe, but it costs automation rate.
- **The keyword rules are crude.** "URGENT, where's my order?" blocks automation; with the model down, every "urgent" is P1.
- **Long email threads** (quoted history, signatures) aren't stripped, so accuracy and cost degrade.
- **A provider outage** sends everything to people. That's safe, but it floods the queue and needs an alert.
- **With the model, on the sample** (Groq `gpt-oss-120b`): 0 unsafe and 0 missed automations, 25/25 routes; optimistic, as above.
- **Groq's free plan** allows 8,000 tokens a minute. At 8 parallel calls, 18 of 25 messages ran out of retries and went to people; the sample needs `--concurrency 1`, and production at 10,000 a day needs a paid plan.

**With another day**
- **Shadow mode.** Run beside agents for two weeks, then enable automation intent by intent once precision meets a target (e.g. 98%).
- **Calibration.** Label about 500 real messages and replace the 0.80 bar with a measured one.
- **Context.** Sender and thread lookup, an order-ownership check, voice-note transcription.
- **Privacy.** Redact personal data before the LLM; a DPA with zero retention; India/Canada data residency.
- **Monitoring.** Route mix, override rate per intent, a sampled audit of automated decisions.

**AI tools used**
- **Claude Code (Claude Opus 5.5):** reviewed my first plan against the data, wrote most of the code and tests, and drafted the README and this log. I reviewed, ran and edited the result.
