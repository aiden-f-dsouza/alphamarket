# Competitor teardown: Dub (dubapp.com), 2026-10-01

*Working reference for Aiden and Kyle while building Koine. Compiled from
dub's help center (130 articles via its public Zendesk API), dub Advisors'
Form ADV Part 1 and 2A (filed 2026-06-11), Form CRS (2026-06-08), FINRA
BrokerCheck, SEC EDGAR (Form D, X-17A-5), App Store / Google Play, press,
and the Massachusetts consent order. Not legal advice. **[VERIFY]** = single
or conflicting source; do not build on it. Reddit and TikTok were not
reachable, so user sentiment is under-sampled. Companion to
`regulatory_reference.md` §2c, which already names Dub as "the template".*

---

## 0. The verdict in five lines

1. **Dub is a vertically integrated brokerage, not a social app.** Parent
   DASTA Inc. owns an SEC RIA (dub Advisors) and a FINRA broker-dealer (dub
   Financial) clearing through Apex. That licence stack took about 2.5 years
   to build before launch, and it is their moat.
2. **Their weakness is our thesis.** Every headline number is gross of fees
   and TWR from a per-portfolio inception date. Discovery only shows
   portfolios that beat VTI, and their own documents call the same numbers
   both "actual" and "hypothetical". Massachusetts fined them **$500k** in
   March 2026 for performance advertising. "Verified" is a phrase they use;
   it is not a methodology.
3. **They only verify what happens inside dub.** A track record built at
   Kalshi, Kraken, IBKR or Alpaca does not exist on Dub. Koine's
   cross-venue, read-only verification is a different product: Dub is "copy
   inside our broker", Koine is "prove it wherever you trade".
4. **The real overlap is the consumer demand ("pick people, not stocks"),
   not the product.** Dub executes and Koine is paper-only (invariant 8). Dub
   wins any investor who wants money moved today. We win the creator who
   wants a portable, credible record, and the investor who wants to check
   before trusting.
5. **The threat to watch:** Dub adding crypto (zerohash launch partner,
   July 2026) or event contracts (named as "future expansion" in 2025). Both
   would move them onto our venues. A "connect an outside brokerage to import
   your track record" feature would hit our core directly; nothing indicates
   they are building it.

---

## 1. Corporate and regulatory structure

| Entity | Role | IDs | Notes |
|---|---|---|---|
| DASTA Incorporated (DBA dub) | Parent, app owner, App Store seller, bills the platform subscription | EDGAR CIK 0002070295; Delaware | Founded 2021 as **Echofi**. CEO Steven Wang (Harvard dropout, ex-Apple Watch PM), COO Brett Chereskin. Board: Hans Tung (Notable), Jordan Nof (Tusk), Michael Acheson. |
| dub Advisors LLC (fka DASTA Investments, Echofi Investments) | SEC RIA. Discretion over Premium portfolios. "Sole fiduciary." | CRD 315589, SEC 801-122735; registered 2021-11-23 under the **internet adviser exemption (Rule 203A-2(e))** | RAUM **$81.8M, 27,037 accounts** (12/31/2025), all discretionary. 8 employees at the RIA. CCO/CEO Justin Ramirez, Head of Investments William Wise. |
| dub Financial LLC (fka DASTA Financial) | FINRA introducing broker-dealer; executes self-directed copies | CRD 316477, SEC 8-70841; FINRA approved 2022-12-13 | Fully disclosed to **Apex Clearing**. $125k clearing deposit, **$15k/month minimum clearing charge**. Net capital about $341k. No disclosures. |

**Two legal tracks for "copying" (the key design pattern):**

- **Premium portfolios** (creator-licensed, politician, 13F, AI, thematic)
  are *discretionary model-portfolio management by the RIA*. Creators are
  "third-party model providers under a data-licensing arrangement". They are
  not registered and are "compensated under a licensing or revenue-share
  structure". The RIA reviews every update and "may adjust… block
  transactions".
- **Core / user portfolios** are *self-directed brokerage*. The copier
  authorises dub Financial to mirror orders, and nobody supervises. Creators
  "are not investment advisers". Core creators have **no documented pay**.

That split is how they pay creators without making creators register: pay
only flows where the RIA holds discretion. It confirms the reading behind
our invariant 8 and the "Dub pattern" in `regulatory_reference.md` §5
Pivot 1.

**Conflicts disclosed:** Apex principals own part of DASTA (Bill Capuzzi,
Apex CEO, was a seed angel). Some users are DASTA investors with copyable
portfolios. Kian Saidi, a top creator, may be a "partner at dub advisors"
**[VERIFY]**.

**Enforcement:** Massachusetts Securities Division, docket **E-2025-0072,
2026-03-30**.
- What they did: Instagram ads (Dec 2024 to Mar 2025) showed *intraday*
  performance of the Pelosi and Buffett copy portfolios, with disclosures
  only in the comments. Internal messages admitted the daily figures went
  "stale the next day and thus misleading". The ads ran 11+ days after the
  CCO ordered them pulled.
- Penalty: $500k, censure, restitution, and policy changes.
- Their Form CRS still answers "No" to disciplinary history while ADV Item 9
  discloses the order, so their own documents disagree.
- No SEC or FINRA actions were found.

---

## 2. Revenue model

Three layers, from three entities:

| Layer | Who bills | Price (as of 2026-10) | Notes |
|---|---|---|---|
| **Platform subscription** | DASTA Inc. (software, not an advisory fee) | App Store: $9.99–12.99/mo, $75.99–119.99/yr. Price shown only at checkout. | Required since about May 2026 to copy, auto-copy or create. Legacy "Founder's Access" ($9.99/mo or $89.99/yr, all creators included) is closed to new users. A free tier lets you browse and make a one-time static copy of Core portfolios; "dub Plus" adds auto-copy **[VERIFY tier name and price]**. 7-day trial. About 50k paying subscribers in May 2025. |
| **Advisory fee on Premium portfolios** | dub Advisors (RIA) | **0–2.50%/yr of average daily NAV**, billed quarterly in arrears and deducted from the account (positions may be sold to pay it). Legacy: **$25–500 per Premium portfolio** per quarter or year. | The asset-based fee was introduced for users joining on or after 2026-06-11, and dub expects "an increasing portion" of portfolios to move to it. The rate is "set, in part, by reference to fees payable to the Premium Creator". The help center also shows ".25%–4%" **[VERIFY, conflicting]**. |
| **Brokerage economics** | dub Financial (BD) | No commissions | Share of Apex's **PFOF**, explicitly including Premium-portfolio trades. Share of **cash-sweep interest** (opt-out). Pass-throughs: SEC/TAF fees, $30 ACH return. Securities lending not mentioned. BD income statement is confidential. |

**Creator take:** TechCrunch (Feb 2025) reported that dub takes **25% of
creator management fees**, which implies about 75% to the creator. No dub
document states a split **[VERIFY]**. At the 2025 launch, pay was "a scaling
fee that takes into consideration several social metrics", negotiated per
creator.

**What this means for Koine:**
- Dub's mature model is **AUM-based**: the RIA charges a percentage of
  assets and shares it with the creator. That is exactly what D8 rules out
  for us, because we are not an adviser and percentage-of-assets is
  investment compensation.
- Our ceiling is flat subscriptions plus a flat royalty share of
  subscription revenue. Creator earnings at Koine cannot scale with
  copier assets the way Dub's can until a licensed party is in the loop.
  Be honest with creators about this; do not imply parity.
- PFOF and sweep interest exist only because Dub is the broker. They are
  not available to us and should not be part of our plans.

---

## 3. Supply side: how Dub gets creators to list

### Tiers and gates

| | Core (anyone) | Premium (vetted) |
|---|---|---|
| Money in | **$1,000** of own deposits (gains don't count) | **$10k+** own capital ("skin in the game") |
| History | Portfolio active 30+ days before going public | 3+ months in-app (help center) vs **6+ months** (ADV and subscriber guide) **[conflict]**. The record can stay private while it builds. |
| Performance gate | None to publish. To appear on the **leaderboard or Discover**, it must **beat VTI since inception**, have at least 2 rebalances (1 in the last 6 months), no stock at 60% or more, and the creator must have logged in within 4 days. | Beat the S&P 500 by **≥2%** with **max drawdown ≤10%** |
| Diligence | Profile completeness (75-char description, 100-char bio, image) | Background check (felonies, SEC/CFTC/FINRA history), written strategy and risk documents, investment-committee approval. RIA status optional. |
| Capacity | **$15M copy-capital cap** per portfolio, then closed to new copiers | Cap removed |
| Pay | None documented | Licence or revenue share (see §2) |
| Perks | Leaderboard placement | Gold badge, featured placement, white-glove support, richer profile (work history, credentials, FAQ), image posts, subscriber-only posts |

### Mechanics

- **Creators trade their own money** inside dub. The record is real, but it
  only exists on dub.
- Portfolios are **weighted baskets** of US stocks, ETFs and ADRs, with
  fractional shares. No options, crypto, bonds, futures, OTC or foreign
  stocks. Creators who trade options elsewhere express the same view
  through stocks on dub (e.g. $UNUSUAL follows options flow).
- Weights must sum to 100%. Rebalances run only during market hours.
- Community rules: no advice, no "buy the dip" calls to action, no
  misleading claims. dub Advisors edits Premium bios and "monitors
  communications to prevent touting".

### Incentive levers, ranked by importance

1. **Money via AUM-linked fees** (Premium only).
2. **Audience**: leaderboard, Discover rails ("Top Market Beaters", "Skin in
   the Game", "Popular Creators") and the activity feed.
3. **Status**: the gold badge, and the line "become a fund manager without
   the licences".
4. **Zero ops**: dub does the execution, compliance, KYC and billing.

### Who lists

- 200+ creators and 400+ strategies.
- Recognisable names: Lawrence Fuller (ex-Merrill, Seeking Alpha; $FOCUSED
  1.3k copiers, +231.8% gross since 2023-10), Kian Saidi ($UNUSUAL, about
  1.4k copiers, $2.6M copied at peak), Andrew Ver Planck. A few firms
  (Vanta Labs, Eureka Wealth).
- Creators' own capital is modest: the top Premium creator shows $269k and
  dub Advisors itself $62k.
- Supply is thin at the top: a handful of creators carry the copy capital.

### Non-human "strategies" (all Premium, run by the RIA)

| Kind | Examples | How it is built | Honest caveat (theirs) |
|---|---|---|---|
| Politicians | $PELOSI (+267% gross since 2023-09), $TUBERVILLE, $CRENSHAW, $MTGREENE, $JDVANCE, $TRUMP, $ALITO | **Rules-based reconstruction** from STOCK Act filings: recent and large trades weighted more, small positions dropped, the largest capped, calls read as bullish | Filings arrive up to 45 days late, amounts are only ranges, and results are "simulated". Dub uses judgment on edge cases (e.g. did not rebalance after a Pelosi option exercise). |
| 13F | $BUFFETT, $ACKMAN | Rebalanced on each 13F | Up to 45 days after quarter-end. No shorts, derivatives or bonds. |
| Media | $CRAMER / $ANTICRAMER, $FOOLSGOLD, $ALLINPOD | An LLM scores transcripts | Delayed |
| AI | $CHATGPTAI, $CLAUDEAI, $GEMINIAI | An LLM rates stocks; humans check for hallucinations | Risk 5/5 |
| Thematic | $FEDALPHA (rotates into 3x SPXL around Fed meetings), STEEL, ELDERLY… | House rules | Mostly risk 5/5 |

These were their acquisition hook ("Nancy Pelosi is up 123% on Dub with
real capital"), and that hook led to the Massachusetts fine. Their own June
2026 blog now says copying Congress is "up to 45 days delayed" with "mixed"
evidence since 2012, and steers readers to real-time creators instead.

**For Koine: do not build politician or 13F portfolios.** They are
reconstructions, not venue data, so they fail invariant 5. They are also
the exact content that drew the fine. If we ever want the hook, it belongs
in an explicitly separate "public disclosures" section that never touches
the verified badge.

---

## 4. Demand side: how Dub gets investors

- **Eligibility:**
  - 18+, US citizen or permanent resident with an SSN, US address.
  - Individual taxable cash account only: no IRA, no margin, no joint.
  - KYC collects income, net worth and risk profile, which is used to
    recommend Premium portfolios.
- **Minimums:** $100 to copy (marketing) vs a $200 initial deposit (ADV)
  **[conflict]**. Fractional basket buys, so $100 gets the same weights as
  $100k.
- **Funding:**
  - Bank linking via Plaid. Up to $1,000 "Instant Buying Power" right away;
    funds settle in 5 business days.
  - Withdrawals: $50k/day limit, to the originating bank only, with no
    withdrawal fee.
- **Incentives:**
  - A referral programme exists ("financial incentives… to refer family and
    friends"). A third-party site says $20 each to referrer and referee
    **[VERIFY]**. No free-stock offer was found.
  - Paid "solicitor" influencers, disclosed as such.
- **Channels:**
  - Heavy Meta/Instagram paid ads; teens find it through Instagram.
  - Creators bring their own audiences.
  - Large SEO comparison-blog output in 2026 ("eToro alternatives", "best
    real-time copy apps").
- **Positioning:**
  - "From picking stocks to picking people."
  - "Stop underperforming like an average investor."
  - The ultra-wealthy have money managers; now you can too.
  - Against finfluencers: "if someone's wrong on social media, they just
    delete the video."
- **Demographic:** average age 30–35, with growing Gen Z and teen interest.
- **Trust UI:**
  - **Alpha Score 1–5**: factor and sector regression, adjusted for
    confidence and track length, ranked against other dub portfolios, needs
    2+ months.
  - Risk Score 1–5, volatility label, beta bucket, max drawdown and recovery
    time, and a gross-vs-net fee calculator.
  - AI assistant "Arlo".
  - AI "chips" that separate "verified portfolio data" from unverified
    creator text. That last one is a good pattern.
  - Quarterly "is this still right for you?" check-ins.
- **Scale:**
  - 1M+ downloads by May 2025 and "nearly 2M" claimed later.
  - 50k paying subscribers (May 2025).
  - $1B+ in trades (App Store) or $1.6B lifetime (July 2026).
  - Only **$81.8M** of RIA assets. Total brokerage assets are not public.
  - Ratings: iOS 4.7 (11k), Play 4.5 (1.5k), Trustpilot 2.6 (6 reviews).

---

## 5. How trades execute

- **Basket model:** every portfolio is a weighted mini-ETF. Copying buys
  each holding at its weight, in fractional dollar amounts.
- **When a creator rebalances,** dub sends **one aggregated basket order
  covering the creator and every copier**, and fills are allocated at the
  average price. Creator and copiers get the same price at the same moment,
  so neither can front-run the other *inside dub*.
  - This works because the creator's own account is in the same broker. It
    is the single biggest technical advantage of owning the broker-dealer.
  - For Core portfolios dub still discloses that the creator "could be
    trading ahead of you outside of the dub app".
- **Timing:** real time during market hours, typically a couple of minutes.
  Orders outside market hours are queued for the open. The copy is locked
  while a rebalance runs. Settlement is T+1. Order types are probably market
  baskets **[VERIFY]**.
- **Premium:** the RIA reviews each creator update before execution. Legally
  the RIA holds discretion, but Item 16 calls it "limited": the user still
  chooses to copy.
- **Copier controls:**
  - Copy more, partially or fully liquidate, or stop. "Stopping is not
    selling": the holdings stay in the account.
  - Exclude specific tickers platform-wide; the excluded weight stays in
    cash.
  - Recurring investments.
  - No per-position override inside a copy.
- **Capacity:** the $15M cap on Core portfolios guards against market
  impact.
- **Complaints:**
  - Copies sometimes do not match the creator's time or price.
  - Account-level money-weighted return (MWR) differs from the portfolio's
    time-weighted return (TWR). One user saw 25% headline vs about 7%
    actual after adding money mid-period.

**For Koine (paper mirror, `mirror.py`):**
- Dub's benchmark for "fair copy" is *same price as the creator*. Our paper
  fills use the quote at detection time, and that **signal lag is the honest
  difference**.
- Show it as a first-class metric: copier return vs creator return, and the
  median detection lag per venue. Dub cannot show this for anything outside
  its own broker.
- The $15M cap is an idea worth borrowing for paper capacity warnings on
  thin Kalshi and Polymarket books, where a real copier would move the price.

---

## 6. How performance is displayed (where Dub is weakest)

| Practice | Dub | Problem |
|---|---|---|
| Return basis | TWR on portfolio pages and Discover, MWR at account level | Users confuse the two (see the 25% vs 7% complaint above) |
| Fees | **Gross** everywhere; the leaderboard ranks on gross TWR | Net is shown only through a static example and a calculator |
| Period | Each portfolio's own inception date ("inception dates vary") | No standard periods, so portfolios are not comparable |
| Selection | Discover and leaderboard show **only portfolios beating VTI since inception**. Premium requires S&P +2%. A record can stay private until it looks good. | Built-in survivorship and selection bias. No disclosure of what happens to closed portfolios' records. |
| "Real vs hypothetical" | The FAQ says "actual returns achieved with real money". The index-value article says "hypothetical performance". The politician pages say "simulated". The site footer says "not actual… illustrative". | Contradictory, and the MA order turned on exactly this kind of issue |
| Dividends | Included on Discover, **not** on individual portfolio pages | The same portfolio shows two different numbers |
| Short windows in ads | Intraday numbers in ads | The source of the $500k fine |

**This is where Koine differentiates. Each item below either already holds
or is a build item:**

1. Net of venue fees by construction: fees are ledger legs. Keep it that
   way, and say so on every card.
2. Standard periods (1M, 3M, YTD, 1Y, all-time) on every public number. No
   intraday figures in anything shareable.
3. One return definition per surface. If a page shows both TWR and MWR,
   label both.
4. **No performance gate on discovery.** Rank on risk-adjusted metrics with
   a minimum history, never on "beat the index".
5. **Survivorship: we have the same gap today.** `public.py` hides
   delisted, unlisted and quarantined strategies entirely. A creator who
   unlists a losing strategy erases it. Proposed fix: a creator's public
   profile lists retired strategies with their final verified stats (a
   tombstone), and the methodology page states it. Dub cannot do this
   without hurting its funnel; we can.
6. Never use the words "simulated" or "hypothetical" for verified numbers.
   Paper-mirror results are explicitly labelled "paper", a separate category
   that never shares a chart with verified results.
7. Adopt their good idea: a visual split between "verified data" and
   "creator says".

---

## 7. Pros and cons

**Dub's strengths**
- Real execution with real money at $100, fractional shares, and the
  creator and copiers filled at the same price. For the user, it simply
  works.
- Full licence stack (RIA + BD) and a clean legal answer for paying
  creators. This is years of lead time and hard to copy.
- A two-sided marketplace with audience-bringing creators. Hooks
  (politicians, AI, Cramer) that perform well on social media.
- $47M raised, notable investors (Notable, Neo, Tusk), and an Apex-aligned
  cap table.
- Revenue on three layers: subscription, AUM fees, and PFOF/interest.
- Polished trust UI (Alpha Score, risk score, drawdown, check-ins) and
  responsive support.

**Dub's weaknesses**
- **Performance integrity:** gross, per-inception, survivorship-filtered,
  "actual" and "hypothetical" at once. Already fined for it. One more
  regulator action would put the brand at risk.
- **Walled garden:** a record exists only if it was built on dub. Serious
  traders with years elsewhere start from zero and must park $1k–$10k with
  dub to begin.
- **US stocks and ETFs only**, taxable accounts only. No options, crypto,
  event contracts, IRAs or margin.
- **Fee drag on small accounts:** about $90–120/yr is about 5% on a $2k
  account, plus up to 2.5% AUM on Premium. eToro US CopyTrader is free and
  Autopilot is $100/yr per portfolio.
- **Lagged hooks:** the politician and 13F content is stale by design, and
  they now play it down themselves.
- **Pricing churn:** flat, then per-creator, then AUM, plus a hidden
  checkout price and a closed legacy plan. Reviews complain about
  cancellations.
- Thin team at the RIA (8), a thin top tier of creators, and a modest BD
  balance sheet against a $15k/month Apex minimum.

**Where Koine is weaker than Dub (be honest)**
- We do not move money, so the "make my money do what they do" job is
  unserved. Paper tailing has to stand on information and engagement, not
  returns.
- We cannot pay creators in proportion to the capital following them.
- Five of our venues need written partner approval before production
  connects. Dub controls its own data.
- No distribution yet. Dub's acquisition engine (paid social plus creator
  audiences) is proven.

---

## 8. Wider landscape (for positioning)

| Player | Model | Fee | Relevance |
|---|---|---|---|
| **eToro CopyTrader (US, since 2025-10-29)** | Self-directed BD, copy "as a tool"; stocks, ETFs, crypto; up to 100 traders | No copy fee (PFOF/spreads) | The free alternative that undercuts Dub's pricing |
| **Autopilot** | RIA trading in the user's *existing* broker (Plaid; Public is the default since Jan 2026); politicians and funds | $100/yr or $29/qtr per portfolio, $500 minimum | $1B+ AUM claimed. Closest to our broker-agnostic stance, but it executes. |
| **Composer (by SoFi, since June 2026)** | Rules-based algorithmic "symphonies", not people | About $40/mo | Shows big incumbents buying this category |
| **Collective2** | CFTC/NFA-registered; strategies auto-traded at the user's broker | $199–299/mo plus leader fees | The futures and CTA precedent for our prediction-market path |
| **Iris** | Social app with linked brokerages, manual copy | Free | Small and possibly dormant **[VERIFY]**. The closest to "verified social" without execution. |
| **Polycopy, Kreo** | Polymarket (and Kalshi) trader ranking and copy bots | Varies | Direct competitors on our prediction-market venues. They work because Polymarket is on-chain. |
| **Kalshi native** | Social feed plus opt-in leaderboard | — | The venue as competitor (`regulatory_reference.md` §5 Pivot 2) |

---

## 9. Build implications, in priority order

1. **Survivorship tombstones** (§6 item 5): show retired strategies on the
   creator profile with their final verified stats. A small change in
   `public.py` and the templates, plus a methodology paragraph.
2. **Comparable periods everywhere:** standard windows on cards and the
   marketplace table. Never rank on all-time-since-inception alone.
3. **Copy-fidelity metric for paper tails:** creator vs copier return and
   detection lag, per strategy. This is the honest version of Dub's "same
   price" claim, and it works across venues.
4. **Share-card discipline:** the MA order is the spec.
   - No intraday or short-window numbers.
   - Period, "net of venue fees" and the disclaimer are baked into the
     *image*, not the caption.
   - A test that every share card renders them.
5. **Discovery without a performance gate:** minimum history and activity,
   then risk-adjusted ranking. A "skin in the game" analogue already exists
   for free in our data: show capital at risk from the ledger.
6. **"Verified" vs "creator says" labelling** on strategy pages, Dub-style,
   including any LLM-written descriptions (already required to be
   descriptive by `regulatory_reference.md` §2a).
7. **Creator pitch copy:** "Bring the record you already have. We verify it
   read-only wherever you trade. Dub makes you start over with $1k–$10k on
   their app." This targets the serious-trader segment Dub cannot onboard.
8. **Do not build:** politician or 13F portfolios, AI-picked portfolios,
   AUM-linked creator pay, or any phrasing that implies execution.

---

## 10. Open questions [VERIFY]

- Current dub Plus / platform price at checkout, and whether a free tier can
  still copy anything.
- The creator revenue split (the 25% figure is a single 2025 press report).
- The asset-fee range: 0–2.5% (ADV) vs 0.25–4% (help center).
- Premium track-record minimum: 3 vs 6 months.
- Referral reward amount, and whether one is currently running.
- Order types on basket trades.
- Total brokerage assets (only RIA RAUM is public), revenue, valuation.
- The Quantbase Investments link (books kept at their address).
- Whether Kian Saidi is affiliated with dub Advisors.
- Crypto launch status via zerohash, and any event-contract plans.
- Reddit and TikTok sentiment (not reachable in this pass).

---

## Key sources

- Form CRS (dub Advisors, 2026-06-08): https://reports.adviserinfo.sec.gov/crs/crs_315589.pdf
- Form ADV Part 1: https://reports.adviserinfo.sec.gov/reports/ADV/315589/PDF/315589.pdf
- Form ADV Part 2A: https://files.adviserinfo.sec.gov/IAPD/Content/Common/crd_iapd_Brochure.aspx?BRCHR_VRSN_ID=1048105
- BrokerCheck, dub Financial: https://api.brokercheck.finra.org/search/firm/316477
- BD statement of financial condition: https://www.sec.gov/Archives/edgar/data/1899305/000189930526000001/dastapublic25.pdf
- Form D (Series A): https://www.sec.gov/Archives/edgar/data/2070295/000207029525000001/primary_doc.xml
- MA consent order coverage: https://finance.yahoo.com/markets/stocks/articles/galvin-fines-online-poster-promised-192958700.html and https://www.sullivanlaw.com/viewpoints/misleading-social-media-posts-can-cost-you-500000-in-massachusetts
- Help center, Premium Creator Program: https://support.dubapp.com/hc/en-us/articles/36023144673691
- Help center, Creating your own portfolio: https://support.dubapp.com/hc/en-us/articles/35784594156443
- Help center, Leaderboard criteria: https://support.dubapp.com/hc/en-us/articles/48361633496603
- Help center, Performance calculation: https://support.dubapp.com/hc/en-us/articles/29995073697307
- Help center, Management fees: https://support.dubapp.com/hc/en-us/articles/45456546526491
- Help center, Fee schedule: https://support.dubapp.com/hc/en-us/articles/23940902033819
- Help center, Politician methodology: https://support.dubapp.com/hc/en-us/articles/20011390716187
- Help center, Alpha Score: https://support.dubapp.com/hc/en-us/articles/45241507003675
- Help center, $15M cap: https://support.dubapp.com/hc/en-us/articles/40202443971099
- Help center, In-app disclaimers: https://support.dubapp.com/hc/en-us/articles/18043799243931
- TechCrunch (Feb 2025, 25% cut): https://techcrunch.com/2025/02/02/dub-the-copy-trading-app-that-has-teens-talking/
- TechCrunch (Series A): https://techcrunch.com/2025/05/01/buzzy-investing-app-dub-lands-30m-series-a
- Seed PR: https://www.prnewswire.com/news-releases/first-ever-copy-trading-platform-dub-raises-17m-seed-round-from-a-list-vcs-and-individual-investors-302067967.html
- Creator program PR: https://www.prnewswire.com/news-releases/dub-launches-top-creator-program-for-retail-investors-302352768.html
- dub blog, copying Congress: https://www.dubapp.com/blog/how-to-copy-congress-stock-trades
- App Store: https://apps.apple.com/us/app/dub-copy-trade-real-investors/id1598920501
- eToro US CopyTrader: https://investors.etoro.com/news-releases/news-release-details/etoro-brings-copytradertm-us-empowering-investors-trade-smarter
- Autopilot + Public: https://www.prnewswire.com/news-releases/public-and-autopilot-partner-to-bring-a-new-model-of-managed-investing-to-retail-investors-302672071.html
