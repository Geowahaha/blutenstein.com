# Blutenstein Trusted SME Network — SuccessCasting Pilot

## Verdict
Blutenstein must not become another ads/feed spam engine. The wedge is a trust-and-intent operating system: customers describe a real job, Blutenstein verifies intent and routes them to pre-vetted SME operators with evidence, professional follow-up, and accountability. For SMEs, Blutenstein is not only lead generation; it is a premium sales and trust layer that removes the pain of paying Google/Facebook for noisy, scam-prone sponsored traffic.

## Problem to replace
Traditional ad feeds create four pains:
1. Sponsored posts appear repeatedly without enough filtering.
2. Customers cannot tell who is real, skilled, available, or safe.
3. SMEs pay for clicks before knowing whether the person has a real job.
4. Follow-up is noisy: line blasts, generic cold DMs, and no professional context.

## New category
Blutenstein = Trusted SME Matchmaking Network.
Not social media. Not directory. Not simple chatbot.

It has three promises:
1. Verified operator: SME profile is checked by work evidence, real contact routes, service boundaries, and response behavior.
2. Verified intent: lead must show buying intent, timing, job details, or a traceable inquiry path before escalation.
3. Respectful sales: outreach is specific, polite, opt-out aware, and offers useful next steps instead of spam.

## Core algorithm: T.R.U.S.T. Score
Each opportunity gets a Trust Match Score, not just a lead score.

### T — Timing intent
Signals:
- Asked for price/quote/delivery.
- Mentions urgent date, breakdown, production stoppage, replacement, or repair.
- Recent inbound chat/form/RFQ/search-query capture from first-party pages.
- Repeated visits to service/product pages.

### R — Relevance to SME capability
Signals:
- Product/service words: pulley, มู่เล่ย์, casting, หล่อเหล็ก, โรงหล่อ, FC/FCD, shaft hole, keyway, machining.
- Industry fit: factory, machine repair, rice mill, conveyor, automotive, food factory, agricultural machinery.
- Geography/logistics fit.

### U — Urgency and value
Signals:
- Downtime words: เครื่องหยุด, ไลน์ผลิตหยุด, ด่วน, today/tomorrow.
- Quantity/recurrence: multiple pieces, maintenance schedule, OEM/stock part.
- Has drawing/photo/sample.

### S — Safety and trust risk
Negative signals:
- No business identity.
- Requests suspicious payment/credential flow.
- Unclear need, copy-paste spam, unrelated consumer query.
- Fake-looking profile or mismatched company/domain.

### T — Touch strategy
Output is not “send LINE”. It decides the professional contact mode:
- Warm consult: if high intent and high relevance, call/LINE with concise technical question.
- Helpful education: if medium intent, send a short guide/checklist.
- Nurture: if weak timing, save to watchlist and re-contact only on new signal.
- Reject/quarantine: if scam/noise.

## SuccessCasting pilot ICP
Primary target jobs:
1. Pulley / belt pulley / V pulley / มู่เล่ย์
2. Iron casting / steel casting / หล่อเหล็ก / หล่อโลหะ
3. Machine spare part replacement from sample/photo/drawing
4. Low-volume custom casting, 1 piece upward
5. Factory repair/maintenance teams that need fast advice

Required qualification fields:
- Work item: pulley/casting/spare part/etc.
- Material or grade if known.
- Size/weight/dimensions.
- Shaft hole / keyway / groove type for pulley.
- Quantity.
- Drawing/photo/sample availability.
- Deadline/urgency.
- Contact route.

## Data model: first-party intent graph
Blutenstein cannot legally/ethically know private Facebook-style behavior unless data is first-party, consented, imported, or from official APIs. So the replacement strategy is to create a better first-party graph:

### First-party sources
- SuccessCasting site search and AI chat turns.
- RFQ forms and uploaded photos/drawings.
- Product/service page visits with consented analytics.
- LINE OA webhook events from users/groups that message the bot.
- Email replies and quote outcomes.
- Sales notes: won/lost/reason/next date.

### Public/official sources
- Google Places API when configured.
- OpenStreetMap fallback.
- Public company websites and contact pages.
- Meta/LinkedIn official API or manual import/export only; no credential-gated scraping.

## Professional outreach protocol
Every outreach must pass this checklist:
1. Specific reason for contact: “เห็นว่าบริษัทอยู่ในกลุ่มโรงงาน/ซ่อมบำรุงที่อาจมีงานมู่เล่ย์/อะไหล่เครื่องจักร”
2. Honest capability: no fake guarantee, no fake discount.
3. One helpful technical question.
4. Low pressure: invite if relevant, allow ignore/opt-out.
5. Clear next step: send photo/drawing/size/quantity/deadline to @SCNW.

Template:
สวัสดีครับ ผมติดต่อจาก SuccessCasting โรงหล่อ/งานหล่อโลหะและมู่เล่ย์ตามแบบครับ
ที่ติดต่อเพราะเห็นว่าธุรกิจของคุณอยู่ในกลุ่มโรงงาน/เครื่องจักรที่อาจมีงานซ่อมบำรุงหรืออะไหล่หล่อตามแบบเป็นครั้งคราว
ถ้าตอนนี้มีงานมู่เล่ย์, เหล็กหล่อ, หรืออะไหล่เครื่องจักรที่ต้องทำจากตัวอย่าง/รูป/แบบ ทีมเราช่วยประเมินแนวทางผลิตเบื้องต้นได้ครับ
คำถามเดียวเพื่อไม่รบกวน: ตอนนี้มีชิ้นงานที่ต้องการประเมินราคา/ผลิตภายใน 30-60 วันไหมครับ?
ถ้ามี ส่งรูปหรือขนาดคร่าว ๆ ที่ LINE @SCNW ได้เลยครับ ถ้าไม่เกี่ยวข้องสามารถข้ามข้อความนี้ได้ครับ

## Product surfaces
### Customer side
- “Find trusted SME” intake page: customer states job, deadline, location, budget range, and file/photo.
- Blutenstein guarantee display: vetted SME, evidence, response SLA, dispute/help path.
- Offer packet: not an ad; a short evidence-backed recommendation.

### SME side
- Trust profile: proof, capabilities, service boundaries, case examples.
- Intent inbox: ranked by Trust Match Score.
- Outreach assistant: drafts professional message, call script, next question.
- Outcome tracking: contacted, replied, RFQ, quoted, won/lost.

### Blutenstein back office
- Vetting queue for SMEs.
- Lead quality review.
- Scam/noise classifier.
- Marketplace trust ledger: claim → evidence → status.

## Business model
1. SME subscription for trusted profile + intent inbox.
2. Premium verified lead packs only when intent is above threshold.
3. Success fee on closed deals where legally/commercially appropriate.
4. Concierge sales retainer for high-value SMEs.
5. Customer-side free matching to build demand and trust.

## What starts now for SuccessCasting
Phase 1, already implementable:
- Rebrand Blutenstein page around trusted SME network.
- Add Trust Score algorithm spec and API status.
- Expand SuccessCasting lead engine from generic lead list to intent-depth categories.
- Daily LINE report includes why each lead matches and what action to take.

Phase 2:
- Add first-party page analytics with consent.
- Add RFQ intent graph tables.
- Add salesperson workflow: contact script, follow-up date, outcome.
- Add verified SuccessCasting profile page.

Phase 3:
- Customer-facing “find a real supplier” intake.
- Multi-SME recommendation packets.
- Reviews/evidence and trust guarantees.
