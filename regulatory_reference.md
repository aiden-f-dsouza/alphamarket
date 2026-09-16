# Koine / Tails — Regulatory & Strategy Reference

*Working reference for Kyle & Aiden. Compiled 2026-09-06 from the regulatory research report (2026-09-02) and the prediction-market pivot analysis (2026-09-03). Not legal advice. Items marked **[VERIFY]** are unconfirmed and need checking with counsel, the venue, or the vendor.*

---

## 0. The three sentences to remember

1. **Verification-only is the safe zone**, but the current MVP violates the Kalshi Developer Agreement as written. Get written permission or change venues.
2. **20%-of-profit carry is dead for retail in every securities path.** The only path where profit-share is legal is full CTA/NFA registration on prediction markets.
3. **Any real auto-execution needs a licensed party in the loop.** For prediction markets, the cheapest licensed party you can become is a CTA. For stocks, partner with an RIA/broker; don't try to become one yet.

---

## 1. Two separate problems, two very different difficulties

| Problem | Type | Difficulty | What fixes it |
|---|---|---|---|
| CTA status when tailing prediction markets | Regulatory (CFTC/NFA) | **Cheap** — weeks, low thousands + counsel | Register as a CTA |
| Kalshi Developer Agreement prohibits aggregating member data | Contractual (business dev) | **Hard** — depends on Kalshi saying yes | Written authorization / Builders partnership / switch venues |

No registration fixes the contract. No contract fixes the registration. Work both in parallel.

---

## 2. Regulatory classification by layer

### 2a. Verification-only MVP (current product)

- **Advisers Act:** Publisher's exclusion (*Lowe v. SEC*, 472 U.S. 181) protects impersonal, bona fide, regularly-circulated information. Public non-personalized track-record pages look like Morningstar/Bloomberg-style information provision. **Gray area:** SEC has an open "information providers" inquiry; boundary is under review.
- **SEC Marketing Rule (206(4)-1):** Only binds registered advisers. Doesn't bind Koine directly unless Koine becomes an adviser. Matters if (a) you pivot to adviser status later, or (b) an RIA creator points clients to their Koine page. Adopt its discipline anyway: net returns, standardized periods, actual vs hypothetical labeling.
- **CTA angle:** Kalshi contracts are CFTC commodity interests. Impersonal, non-tailored historical display is analogous to the newsletter carve-out (Rule 4.14: advice "not based upon knowledge of or tailored to a customer's particular account"). Stay descriptive, stay out.
- **LLM strategy card is a risk vector.** Descriptive/backward-looking ("concentrates in short-dated weather markets") = fine. Anything recommendation- or suitability-shaped ("good for conservative investors") = advice. Keep it descriptive, human-reviewed.

### 2b. Tailing prediction-market positions (Kalshi / Polymarket US)

- Mirroring a leader's trades for compensation = **CTA activity**. Likely makes the compensated, publicly-held-out leader a CTA too.
- **De minimis exemption** (CEA 4m(1) / Rule 4.14(a)(10)): <15 clients in 12 months **and** not holding out to the public. A public marketplace page = holding out (CFTC Letter 97-26: unless clients are restricted to family/friends/existing associates, you're holding out). Unusable for a public product; usable only for an invisible closed beta.
- **Performance fee worsens the analysis** — profit-share is compensation for advice and removes any "just software" defense.
- **IB/FCM risk** if you solicit orders or handle funds. Keeping funds in the user's own account mitigates, doesn't erase.
- **SignalPush / BareIt Media (CFTC, 2023, $100k):** signals + automation for binary options without CTA registration → unregistered CTA. **This is the closest cautionary tale to "tail via signal + user's write key." Do not build that.**

### 2c. Copy-trading stocks (Alpaca / DriveWealth)

- Every precedent puts a licensed party at the money layer:
  - **Dub:** dub Advisors (SEC RIA) makes all trading decisions; creators license models and earn royalties/subscriptions; BD + Apex clearing. **This is the template.**
  - **eToro USA:** self-directed BD; CopyTrader framed as a non-advisory "tool"; no fee for the tool; monetized via PFOF/spreads. Took years and lawyers; CopyTrader is constrained in the US.
  - **Collective2:** CFTC/NFA registered since 2008; funds stay at user's own broker; subscription pricing (not per-trade) to avoid BD characterization.
- **Adviser status:** leader with discretion/tailored advice for compensation = investment adviser. <$100M AUM → state registration (Illinois Securities Department, Form ADV via IARD, Series 65).
- **Performance-fee wall:** Rule 205-3 qualified client. From June 29, 2026: **$1.4M managed with adviser or $2.7M net worth.** Kills 20% carry for retail. Use flat/subscription/royalty.
- **BD trigger:** transaction-based compensation for routing = broker-dealer. FINRA BD registration is out of reach now.

### 2d. Option B — Freeport model (Privy → Crossmint → stablecoins → on-chain perps)

- Freeport: on-chain, no-KYC, non-custodial, routes perps into Hyperliquid etc.; self-describes as not a BD, not an exchange, not custody "in the regulated sense."
- For US founders this is **riskier, not lighter**: CFTC (retail perps off a DCM — what got Polymarket fined $1.4M in 2022), SEC (tokenized stocks), FinCEN MSB + state money-transmitter law (49 states, six/seven-figure programs, 18 U.S.C. §1960 criminal exposure; OKX paid $504M in 2025).
- Privy/Crossmint are infrastructure vendors; they provide **zero regulatory cover**.
- **Decision: do not pursue.**

### 2e. What "money stays in the user's account" buys you

- **Avoids/reduces:** custody rules, BD customer-funds handling, money-transmitter status.
- **Does NOT avoid:** adviser/CTA status, which turns on advice + compensation, not custody.
- **KYC/AML** sits with the broker/venue partner as long as Koine isn't itself an MSB.
- Stripe/brokerage debit for fees is fine mechanically; the problem is being *entitled* to carry as an unregistered adviser or from non-qualified clients, not the payment rail.

---

## 3. Vendor & cost reference

| Item | Cost | Time | Notes |
|---|---|---|---|
| Entity (DE C-corp/LLC) | ~$500–2,000 | Days | Do this first |
| Fixed-fee CFTC/securities memo | Meaningful five figures at market; get 2–3 quotes **[VERIFY]** | Weeks | Scope narrowly (see §7) |
| **CTA / NFA** | Series 3 ~$140; NFA app $200; annual dues high hundreds; principal/AP fees; disclosure doc w/ counsel ~$5–15k **[VERIFY]** | Weeks–2 months | Cheapest license that permits carry |
| Illinois state RIA | $400 firm + $150/IAR + $20/branch; Series 65 $187 | 2–4 months | No retail carry; heavy compliance for two students |
| FINRA BD | High five/six figures | Many months | Infeasible now |
| Money transmitter | Seven figures, 6–18 mo per tranche | — | Infeasible; avoid |
| Alpaca Broker API | Tiered subscription (Standard / StandardPlus tiers; exact $ **[VERIFY]**) | — | Provides KYC/custody/clearing; **no built-in allocation or advisory-fee calc**; expects a real entity + compliance posture; live multi-user trading likely needs an RIA/BD wrapper |
| DriveWealth | Enterprise **[VERIFY]** | — | Heavier lift than Alpaca |
| Collective2 (as partner) | Rev share | Short | Lightest stock option: creator lists on C2 |
| Atomic | **[VERIFY]** | — | RIA-as-API; possible licensed partner |
| Privy / Crossmint | **[VERIFY]** | — | No regulatory cover |

---

## 4. Path decision matrix

| Path | Registration | Cost | Time | Risk | 20% carry? |
|---|---|---|---|---|---|
| 1. Verification-only + subs/sponsors/creator listing fees | None (if impersonal) | Low | Now | Low — Kalshi ToS is the live risk | N/A |
| 2. Signal + flat sub, **manual** replicate | None if impersonal; CTA if tailored/automated | Low | Weeks | Med — SignalPush drift | No |
| 3. Partner with regulated rail (C2 / RIA model-provider / Alpaca+RIA) | Partner holds licenses | Med | 3–9 mo | Med — partner dependency | No (royalty instead) |
| 4. Own Illinois RIA + model marketplace | State RIA | ~$2–10k + compliance | 3–6 mo | Med-High | No |
| **5. Prediction-market CTA** | CTA + NFA (Series 3) | ~$1–3k + counsel | Weeks–2 mo | High — venue ToS, PM litigation | **Yes (only path)** |
| 6. Non-US-first | Still US-exposed | Varies | — | Very high — jurisdictional trap | No |

---

## 5. Prediction-market pivots (the plan)

### Pivot 1 — Register as a CTA; become the licensed layer

- Series 3, NFA membership, fingerprints/fitness per principal, **disclosure document filed with NFA before soliciting anyone**, recordkeeping, performance-record rules (Rule 4.35), NFA promotional-material rules (Rule 2-29).
- **Structure (Dub pattern applied to futures):** Koine (registered CTA) is the adviser to tailing customers. Creators are signal/model licensors paid a royalty by Koine — not advisers to the public.
- **Biggest open question:** does this keep creators out of CTA registration? If every creator must register, there is no supply side. **First question for counsel.**
- Your verification engine is an asset here: NFA requires supportable performance claims and you already produce them.
- Doesn't fix: order placement into tailer accounts (venue problem, §5 Pivot 3), or the litigation risk to the category (§8).

### Pivot 2 — Reorder venues; reduce Kalshi dependence

- **Kalshi = worst venue for permission:** private accounts, prohibitive Developer Agreement, and they just shipped their own social feed + opt-in leaderboard (i.e., they consider this their feature — biggest threat and the party you need).
- **Polymarket = public on-chain wallets** → verification needs no permission (why Polycopy exists).
  - Polymarket global excludes US persons: can verify non-US traders, can't let US users tail them.
  - **[VERIFY]** whether Polymarket US (QCX, FCM-intermediated) exposes positions on-chain the same way. If yes → natural first venue for both verification and tailing; FCM model is a cleaner place for a registered CTA to plug in.

### Pivot 3 — Turn Kalshi into a partner

- Apply to **Kalshi Builders** ($2M grants, Solana/Base co-sponsored). Pitch as a volume driver, not a data scraper. Let Kalshi's leaderboard remain the canonical public ranking.
- Ask specifically for: (a) written permission for read-only aggregation and display with member consent; (b) an order-routing path — partner API, or whichever FCMs offer Kalshi access with CTA-managed accounts **[VERIFY]**.
- If they decline: **stop ingesting Kalshi data.** The agreement binds the member and you operate in their account. Have Pivot 2 ready.
- Note: the widely advertised "Kalshi affiliate program, 20% commission" is a third-party scam (kalshiai.com / kalshispy.com), not Kalshi.

### Pivot 4 — Use the de minimis window deliberately

- Closed, unmarketed tailing beta: <15 clients, friends/existing associates, **no mention of tailing anywhere on the public site**. Public verification pages keep running (impersonal).
- Trap: one "tail this strategy" button on a public page = holding out → exemption gone regardless of client count. Feature must be invisible, not merely gated. Gray area — get counsel to bless the boundary.

### Drop

- "Signal + automation with the user's own write key." That is SignalPush. Register or keep it manual.

---

## 6. Kalshi Developer Agreement — key clauses

- Use "expressly limited to facilitating a member's own trading."
- Prohibits collecting/caching/aggregating/storing data except for your own trading; sharing data with third parties without written authorization; "facilitating trading or account creation by other members."
- NY governing law; $50 aggregate liability cap; terminable at will.
- Source: https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf

---

## 7. Questions for counsel (scope the fixed-fee memo to these)

1. Does our verified-ranking + descriptive LLM card qualify for the publisher's exclusion, or does anything make us an adviser or CTA?
2. Given SignalPush, which exact features flip us from "software/publisher" to CTA?
3. **If Koine registers as a CTA and pays creators royalties for licensed signals, do creators stay out of CTA registration?**
4. Where precisely is the "holding out" line for a closed tailing beta running alongside a public verification site?
5. Can we lawfully take a flat subscription from creators/investors without becoming an adviser? A profit-share?
6. Compare RIA-model-provider vs C2-listing vs Alpaca+partner-RIA for the stock path — which minimizes our registration footprint?
7. What disclaimers and recordkeeping do we need today?

## Questions for Foundable mentors

- Intros to fintech regulatory counsel offering fixed-fee startup memos?
- Any RIAs/BDs/FCMs in the network open to a model-marketplace or CTA-managed-account partnership?
- Warm intro to Kalshi's Builders/partnerships team?

---

## 8. Enforcement cautionary tales (features that triggered liability)

- **SignalPush / BareIt Media (CFTC 2023, $100k):** signals + automation without CTA registration.
- **ProTrade / Marks (CFTC 1996):** subscription commodity advice online, unregistered → cease-and-desist + refunds. Subscription doesn't save you if you're advising on commodity interests and holding out.
- **Mobius Risk Group (2018, $75k), Hendershott ($75k):** "held itself out to the public" / >15 clients / tailored advice.
- **Polymarket (CFTC 2022, $1.4M):** unregistered event-contract platform serving US persons.
- **SEC Marketing Rule sweep (Sept 2024, 9 RIAs, $1.24M):** unsubstantiated claims, testimonials/ratings without disclosures.
- **FINRA finfluencer fines:** M1 Finance $850k, Open to the Public $350k, TradeZero $250k — unsupervised social content.
- Recurring triggers: cherry-picked performance, undisclosed paid promotion, unsupervised social content, automation without registration, public solicitation while claiming an exemption.

## Category risk (prediction markets)

- Third Circuit affirmed Kalshi vs NJ (April 2026, sports contracts are CEA swaps, preempt state law). Maryland denied relief. Michigan temporarily blocked Kalshi sports contracts (June 2026). Ninth Circuit pending (Nevada). New CFTC probe of Polymarket (June 2026).
- A CTA license makes you CFTC-compliant; it does not protect you if states win and venues shrink. Keep the stock/RIA-partner path alive as a hedge.

---

## 9. Sequencing

**Now (September):**
- [ ] Form entity.
- [ ] Email Kalshi Builders with the two specific asks (§5 Pivot 3). Log the response.
- [ ] Confirm what Polymarket US exposes on-chain **[VERIFY]**.
- [ ] Lock product into the safe zone: impersonal, backward-looking stats; no recommendations; LLM cards descriptive + human-reviewed; prominent "not advice / not a broker or adviser / past performance ≠ future results"; net returns; standardized periods.
- [ ] Revoke the read+write prod Kalshi key (still open per BUILD_PLAN).

**Next 60 days:**
- [ ] Get 2–3 fixed-fee quotes; one consult scoped to §7 Q3 and Q4.
- [ ] Decide first venue for tailing based on Kalshi's answer + Polymarket US on-chain findings.

**If counsel answers are workable AND a venue gives a routing path:**
- [ ] Series 3 (both founders or at least one principal).
- [ ] NFA application, fingerprints, disclosure document.
- [ ] Target: registered CTA before end of academic year.
- [ ] Creator comp = royalty from Koine. Investor fee = performance fee above HWM (legal only inside CTA registration).

**If neither venue gives a routing path:**
- You are a verification + social product. Monetize via subscriptions / creator listing fees / sponsorships (no outcome-linked comp). Honest pitch to Foundable: the marketplace depends on a venue partnership under active negotiation.

**Thresholds that change the plan:**
- Kalshi grants or denies written permission.
- You want auto-execution → licensed partner or CTA registration.
- You want profit-share → CTA registration (prediction markets) or drop carry (securities).
- $100M AUM → SEC not state RIA.
- Touching stablecoin/crypto flows → MSB/MTL. Avoid.

---

## 10. Unverified items (do not build on these until checked)

- Alpaca Broker API tier pricing, per-account costs, and whether they onboard pre-seed teams without RIA/BD status.
- DriveWealth minimums.
- Actual fixed-fee ranges from counsel.
- Whether Kalshi will grant written third-party read/display authorization.
- Which FCMs (if any) offer Kalshi or Polymarket US access with CTA-managed accounts.
- Whether Polymarket US positions are publicly on-chain.
- Collective2's current trade-leader registration treatment and partner terms.
- Privy/Crossmint pricing.
- Illinois-specific RIA nuances beyond the NASAA model.
- Exact NFA annual dues and per-principal fees (figures above are approximate).

## Key sources

- CFTC CPO/CTA exemptions: https://www.cftc.gov/sites/default/files/tm/tmcpo_cta_exemptions.htm
- CFTC Letter 97-26 (holding out): https://www.cftc.gov/sites/default/files/tm/letters/97letters/tm97-26.htm
- NFA CTA registration: https://www.nfa.futures.org/registration-membership/who-has-to-register/cta.html
- SEC Marketing Rule text: https://www.law.cornell.edu/cfr/text/17/275.206(4)-1
- Kalshi Developer Agreement: https://kalshi-public-docs.s3.amazonaws.com/Kalshi-Developer-Agreement.pdf
- Kalshi Builders: https://kalshi.com/builders
- Dub Advisors disclaimers: https://support.dubapp.com/hc/en-us/articles/39559653140251-dub-Advisors-In-App-Disclaimers
- eToro CopyTrader guide (Jan 2026): https://www.etoro.com/wp-content/uploads/2025/10/CopyTrader-Guide_Jan-2026.pdf
- Collective2 brokers: https://trade.collective2.com/compatible-brokers
- Cadwalader on SignalPush / CTA definition expansion: https://www.cadwalader.com/fin-news/index.php?nid=78&eid=623
- Polymarket US amended order of designation: https://www.prnewswire.com/news-releases/polymarket-receives-cftc-approval-of-amended-order-of-designation-enabling-intermediated-us-market-access-302625833.html
- Wilson Sonsini on SEC "information providers" inquiry: https://www.wsgr.com/en/insights/informationor-advice-sec-regulation-of-information-providers-may-expand-to-include-providers-of-innovative-investment-analytics.html
