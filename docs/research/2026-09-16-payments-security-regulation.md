# Payments, security architecture, and the crypto/broker data-source delta (2026-09-16)

*Research output feeding docs/platform_plan.md. Not legal advice. Scope excludes CTA/RIA/BD status, the Marketing Rule, publisher's exclusion and Dub/eToro/Collective2 (covered in regulatory_reference.md). Items tagged [VERIFY] are unconfirmed.*

---

## A. Payments and money handling (stay out of money transmission)

### A1. Stripe Billing for subscriptions

**Pricing.** Card processing 2.9% + 30c (US). Stripe Billing is a single plan at **0.7% of Billing volume**. Stripe Tax is **0.5% of volume (no-code/Checkout) or $0.50 per transaction (API)**, charged only in registered jurisdictions. The Customer Portal is included in Billing. Sources: https://stripe.com/pricing , https://support.stripe.com/questions/changes-to-the-stripe-billing-starter-and-scale-plans , https://stripe.com/tax/pricing , https://docs.stripe.com/customer-management

**Webhooks (practical rules).** Verify the `Stripe-Signature` header with the SDK (5 min tolerance, keep clocks synced). Store `event.id` and skip duplicates: Stripe retries for **up to 3 days**, so handlers must be idempotent. Return 2xx fast, process asynchronously. Use idempotency keys on every POST to Stripe. Treat `customer.subscription.updated/deleted`, `invoice.paid`, `invoice.payment_failed` as the source of truth for entitlement, never the checkout redirect. Pin the API version. Sources: https://docs.stripe.com/webhooks , https://docs.stripe.com/api/idempotent_requests , https://docs.stripe.com/billing/subscriptions/webhooks

**Test clocks.** Create a customer with `test_clock` and advance time in sandbox to exercise trials, renewals, dunning and their webhooks. Source: https://docs.stripe.com/billing/testing/test-clocks

**Restricted/prohibited list as applied to Koine (list updated 2026-05-13).**
- *Prohibited*: gambling, "Sports forecasting or odds-making with a monetary or material prize", "Funded prop trading", "Peer-to-peer money transmission", "'Get rich quick' schemes, including investment opportunities that promise high rewards", crypto mining/staking, ICOs.
- *Restricted (prior approval)*: "Investment and brokerage services", money transmitters, "Cryptocurrency exchanges and wallets", lending, BNPL.
- A **track-record/analytics SaaS selling software subscriptions is on neither list**; closest to "financial information services", ordinary SaaS.
- **Review triggers**: copy that reads as an investment opportunity or promises returns, "prediction markets"/"betting"/"picks" on the Stripe-facing site, taking a cut of trading P&L, paying creators in a way that looks like distributing winnings. Keep the Stripe business description as "SaaS: performance verification and analytics for traders; no funds handled, no trading."
Sources: https://stripe.com/legal/restricted-businesses , https://support.stripe.com/questions/prohibited-and-restricted-businesses-list-faqs

### A2. Paying creators

**Stripe Connect account models** (Stripe now steers to controller properties / Accounts v2; trade-offs unchanged):

| | Standard | Express | Custom |
|---|---|---|---|
| KYC/onboarding | Stripe (creator's own dashboard) | **Stripe-hosted onboarding + Express dashboard** | Platform collects |
| Loss liability | Connected account | Platform (Stripe recommends new platforms let Stripe own it) | Platform |
| Per-account fee | none | $2/mo per active account + 0.25% + 25c per payout | same |
| Fit for Koine | OK but creator runs a full Stripe account | **Recommended** | Overkill |

Connect fees: **$2 per monthly active account, 0.25% + 25c per payout**, Instant Payouts 1%, 1099 e-file **$2.99 federal / $1.49 state**. Sources: https://stripe.com/connect/pricing , https://docs.stripe.com/connect/accounts

**Charge patterns.** **Destination charges** when one creator receives one payment; **separate charges and transfers** when a subscription's revenue is split across many creators. Sources: https://docs.stripe.com/connect/charges , https://docs.stripe.com/connect/destination-charges

**Royalty/revenue-share to creators is a mainstream Connect use case.** What matters for money transmission is that the *follower's contract is with Koine* (a software subscription) and Koine then pays creators a licence/revenue share via transfers.

**1099s.** With Express/Custom where the platform sets pricing, **the platform files 1099s** (Stripe e-files). TY2026 after OBBBA: **1099-K back to $20,000 AND 200 transactions; 1099-NEC/MISC threshold $2,000**. A creator revenue share is generally 1099-NEC; confirm with an accountant. Sources: https://docs.stripe.com/connect/tax-reporting , https://www.avalara.com/blog/en/north-america/2025/07/one-big-beautiful-bill-act-1099-reporting-threshold.html

**Alternatives.** Merchant-of-record (Lemon Squeezy, Stripe Managed Payments at ~+3.5% [VERIFY], Paddle 5% + 50c) are for selling your own software; **none pays out to your creators** and Paddle explicitly excludes marketplaces. Mass payouts (Trolley ~$500/mo minimum, Tipalti $99-199/mo entry) only make sense at hundreds of payees. Sources: https://www.lemonsqueezy.com/blog/2026-update , https://www.paddle.com/help/start/intro-to-paddle/does-paddle-support-3rd-party-online-stores

**Structure that keeps Koine out of money transmission.**
1. Every dollar in is payment **to Koine for Koine's own service** (subscription, listing fee). Koine is the merchant; Stripe is the processor.
2. Every dollar out to a creator is a **Koine expense** (licence fee/revenue share) sent as a Connect transfer. Funds sit in Stripe's regulated accounts.
3. Never hold, escrow, pool, or "wallet" follower money; never let followers pay creators directly through Koine; never do holdbacks you control; never move money between users (peer-to-peer transmission, prohibited by Stripe and regulated by FinCEN). Illinois has its own money-transmitter statute; the same "never control funds" rule keeps you outside it [VERIFY current Illinois MTMA status].
Sources: https://www.venable.com/insights/publications/2018/06/money-transmission-in-the-payment-facilitator-mode , https://www.fincen.gov/system/files/2019-05/FinCEN%20Guidance%20CVC%20FINAL%20508.pdf

### A3. Fee models
- **Safe**: flat creator "Pro" subscription, follower subscription for premium pages, creator listing/verification fee, sponsorship, aggregate anonymised data licensing.
- **Avoid**: any fee computed from trades placed, P&L, or "tailed" volume (transaction-based compensation; makes paper tailing look like a product that steers real trading).
- **Venue referral programs (mention only)**: Kalshi pays trading credits (the "20% commission" programs online are third-party scams); Polymarket US has "approved affiliates"; Coinbase affiliate ~50% of fees for 3 months [VERIFY]; Alpaca has no API-partner referral program. Compensation tied to a referred user's trading/deposits is the classic "finder"/solicitation pattern; no SEC finder exemption exists. A flat, non-success-based bounty is least risky. Sources: https://docs.polymarket.us/incentives/referral , https://alpaca.markets/support/does-alpaca-have-a-referral-program-for-its-api-partners-including-revenue-share , https://psbplaw.com/blog/sec-proposed-rule-for-finders/

### A4. Crypto payments: avoid
Accepting crypto into a wallet you control makes you a custodian and, if you convert or forward it, an MSB/money transmitter with state MTL exposure. GENIUS Act (July 2025) adds a federal stablecoin regime but does nothing for a SaaS taking coins. The only tolerable path is Stripe's stablecoin payment method (Bridge/Paxos receives USDC, Stripe settles USD; ~1.5% [VERIFY]); even that adds "crypto" to the Stripe risk profile for near-zero revenue. Skip. Sources: https://stripe.com/legal/stablecoin-payments , https://www.federalregister.gov/documents/2026/08/18/2026-16796/genius-act-regulations-on-payment-stablecoin-issuance-offer-and-sale

**A: Recommendations.** Stripe Billing + Tax + Customer Portal; Connect with Stripe-controlled onboarding and Stripe-owned loss liability (Express shape); destination charges for one-creator purchases, separate charges/transfers for pooled subscriptions; Koine files 1099-NEC via Stripe; no crypto, no wallets, no user-to-user money; keep Stripe-facing copy free of "prediction market/betting/returns" language.

---

## B. Security architecture for many users' read-only keys

### B1. Credential storage
- **Envelope encryption is the industry pattern**: a KMS-held root key wraps per-record data keys; the DB stores `(ciphertext, wrapped_dek, key_version)`. Plaid describes exactly this (AWS KMS root of trust, random data key per payload); SnapTrade says "AWS KMS at rest"; CoinTracker and Koinly say keys are encrypted before storage and not viewable by employees. Sources: https://plaid.com/blog/security-plaid-key-management-system/ , https://www.cointracker.com/security , https://support.koinly.io/en/articles/9489966-is-koinly-secure
- **Today's Fernet vault is fine as the data-key layer**; the gap is a single static `VAULT_KEY`. Upgrade: (1) `MultiFernet` with `rotate()` for zero-downtime rotation; (2) move the root to a KMS and wrap per-record DEKs via `GenerateDataKey`. Source: https://cryptography.io/en/stable/fernet/
- **KMS options**: AWS KMS **$1/key/month, $0.03 per 10k requests, 20k free/month**. Cache unwrapped DEKs in worker memory per poll cycle. Fly.io announced "Fly KMS" as a Sept 2024 beta; current status unknown [VERIFY], do not depend on it. HashiCorp Vault / OpenBao transit gives the same model self-hosted but adds an HA service to run; at two founders, AWS KMS from a Fly app (KMS-only IAM policy) is less ops. HSM custom key stores are unnecessary; AWS KMS default keys are already HSM-backed. Sources: https://aws.amazon.com/kms/pricing/ , https://community.fly.io/t/fly-kms/21825 , https://openbao.org/docs/secrets/transit/

### B2. Compliance frameworks
- **SOC 2**: partners will ask for Type II. Automation platform (Vanta/Drata/Secureframe) **$7.5k-15k/yr**; boutique auditor **Type I $5k-15k, Type II $15k-45k**; pen test $5k-15k. Timeline: 2-3 months to Type I, 3-month observation for first Type II. Do Type I only when a partner asks; start evidence collection now (free). Sources: https://drata.com/learn/soc-2/cost , https://soc2auditors.org/insights/soc-2-type-2-audit-cost/
- **GLBA / FTC Safeguards Rule (16 CFR 314)**: applies to "financial institutions" significantly engaged in activities "financial in nature", including "finders". A consented analytics service is probably not one, but BHCA 4(k) lists financial data processing and FTC guidance is silent on consumer-authorised aggregators: judgment call [VERIFY with counsel]. **Adopt the nine Safeguards elements anyway** (qualified individual, risk assessment, MFA, encryption, service-provider oversight, incident plan, annual report); note **FTC notice within 30 days of a breach affecting 500+ consumers** (effective May 2024). Sources: https://www.ftc.gov/business-guidance/resources/ftc-safeguards-rule-what-your-business-needs-know , https://www.cov.com/en/news-and-insights/insights/2023/11/ftc-finalizes-new-notification-requirement-for-glba-safeguards-rule
- **CCPA/CPRA**: revenue > $26.625M or 100k+ California consumers; not applicable at pre-seed; write the privacy policy as if it were. Source: https://www.cppa.ca.gov/regulations/cpi_adjustment.html
- **NYDFS Part 500**: DFS-licensed entities only; N/A.
- **SEC Reg S-P (2024 amendments)**: binds BDs/RIAs (small firms since June 3 2026) and their vendors. Koine is a consumer-authorised third party, not a BD vendor; expect brokers to cite it when deciding on third-party access. Source: https://www.finra.org/rules-guidance/guidance/sec-regulation-s-p-compliance-date-reminder-20251114
- **CFPB 1033**: enjoined pending reconsideration; NPRM sent to OIRA Aug 2026; scope is Reg E/Reg Z accounts, **not brokerage or exchange accounts**. Directional only: OAuth-based, fee-bearing access becomes the norm. Sources: https://www.cozen.com/news-resources/publications/2026/section-1033-compliance-date-open-banking-rule-enjoined-and-under-reconsideration , https://www.consumerfinance.gov/personal-financial-data-rights/

### B3. App security (practical)
- **Sessions over JWTs** for a server-rendered app: opaque DB-backed session ids (revocable, rotate on login/privilege change), `Secure; HttpOnly; SameSite=Lax`, short idle timeout for the dashboard.
- **2FA**: TOTP (`pyotp`, RFC 6238) mandatory before a user can connect a venue key; encrypted TOTP secrets, hashed recovery codes; WebAuthn later. Step-up re-auth for key connect/disconnect.
- **Rate limiting**: `slowapi`/Redis token bucket on login, signup, TOTP, key-connect, public JSON API; separate per-account budget for outbound venue calls so one user cannot exhaust a venue quota.
- **Audit log**: append-only table (same trigger pattern as `fills`) recording who connected/disconnected/rotated a key, IP/UA, and each credential *decrypt* event with reason; never key material.
- **Outbound IP allowlisting**: egress through a static IP (Fly static egress IP or NAT) so users can whitelist Koine on Binance.US/Kraken/Coinbase. Sources: https://support.binance.us/en/articles/9842812-binance-us-api-keys-best-practices-safety-tips
- **Secret scanning**: `gitleaks` in CI (free) plus GitHub push protection (GitHub Secret Protection is $19/committer/mo on private repos). **Dependency scanning**: `pip-audit` in CI + Dependabot.
- **Postgres**: separate DB roles (web: no SELECT on credential ciphertext; worker: SELECT only), row-level security keyed on `current_setting('app.user_id')`, encrypted volumes + encrypted tested backups stored off-Fly.
- **Incident response**: written plan; Illinois PIPA requires notice to residents without unreasonable delay and to the **Illinois AG when 500+ residents** are affected; other states 30-60 days; FTC 30-day rule if treated as a Safeguards institution. Source: https://law.justia.com/codes/illinois/chapter-815/act-815-ilcs-530/

### B4. Data minimisation
A read-only key still exposes full order/fill history, positions, balances, deposit/withdrawal history, and on some venues PII (Coinbase `/v2/user` returns name/email/country; Kraken exposes no identifying data). Practices: narrowest scope (Coinbase: `wallet:accounts:read`, `wallet:transactions:read`; Kraken: Query Funds/Ledgers/Closed Orders only; Alpaca: no `trading` or `account:write`); store only fields the ledger needs; refuse keys with trade/withdraw permission; on disconnect delete ciphertext immediately and keep only the derived track record with a "source disconnected on <date>" flag; on account deletion purge everything except append-only integrity tables (document retention in the privacy policy). Sources: https://docs.cdp.coinbase.com/coinbase-app/oauth2-integration/scopes , https://support.kraken.com/articles/360043577552-api-security-what-account-information-does-the-api-expose-

**B: Recommendations.** Keep Fernet for record encryption but wrap keys with AWS KMS and adopt MultiFernet rotation; static egress IP; mandatory TOTP before key connect; DB-backed sessions; append-only audit log; gitleaks + pip-audit in CI; adopt the nine Safeguards elements as the security policy; defer SOC 2 until a partner asks, but start evidence collection now.

---

## C. Regulatory delta for crypto exchanges and stock brokers as data sources

### C1. Anything new?
- **Crypto asset status (2026)**: the CLARITY Act failed cloture 49-50 on 2026-09-15. What governs is the **SEC-CFTC joint interpretation of March 17 2026** (Rel. 33-11412) with its token taxonomy stating most crypto assets are not themselves securities, plus GENIUS for stablecoins. For Koine nothing changes structurally: displaying a user's own realised P&L from a venue is not offering, recommending or transacting. If a displayed strategy trades tokens the interpretation would call digital securities, the existing adviser/BD analysis applies; commodity status brings the CTA question already analysed. FinCEN: no funds, no MSB. Sources: https://www.cnbc.com/2026/09/15/senate-cloture-vote-on-clarity-act-fails-dealing-regulatory-setback-to-crypto-industry.html , https://www.sec.gov/files/rules/interp/2026/33-11412.pdf
- **Brokerage data with consent**: Reg S-P and GLBA bind the broker, not the consented third party; the broker's ToS is the binding constraint. **Schwab and Fidelity have cut off credential sharing** and permit third-party access only via OAuth through Akoya or aggregators. **Never ask a user for a broker username/password.** Sources: https://www.investmentnews.com/goria/custodian/schwab-joins-fidelity-in-credential-sharing-crackdown-pontera-escalates-fidelity-blame/262952

### C2. Venue API terms on consented third-party read access
- **Coinbase**: OAuth2 is the sanctioned path but client creation is limited to approved partners; CDP Terms (2026-06-23) prohibit aggregating/storing data beyond permitted purposes and sharing with third parties without written authorisation, which a public leaderboard arguably is. **Plan on written approval or an aggregator.** [VERIFY: clause wording from snippets, page 403.] Sources: https://docs.cdp.coinbase.com/coinbase-app/oauth2-integration/overview , https://www.coinbase.com/legal/developer-platform/terms-of-service
- **Binance.US**: terms say users of a third-party API "may be giving the third-party API provider access to your account" at their own risk (contemplated); global Terms require accounts be used "only for yourself" and Binance has banned accounts using unauthorized tools. Binance.com does not serve US persons. [VERIFY global ToS API clause.] Source: https://www.binance.us/terms-of-use
- **Kraken**: explicitly supports third-party services via user-created keys, recommends minimal permissions, runs a third-party tax-aggregator support section. Friendliest terms. Source: https://support.kraken.com/articles/360041395271-using-the-kraken-api-with-a-third-party-service
- **Alpaca**: OAuth2 designed for third-party apps; read-only is the default scope; approval only for third-party live trading; customer terms make third-party access "solely at your risk". Source: https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api
- **Kalshi** (contrast): Developer Agreement bars sublicensing and aggregation; already handled in regulatory_reference.md.
- Pattern: every venue's terms make the *user* responsible for third-party access. The connect flow should state that, link the venue's page, and get an affirmative tick.

### C3. Testimonial/endorsement rules for a public leaderboard
- **FTC Endorsement Guides (16 CFR 255, revised 2023)**: a displayed track record is a performance claim by Koine. "Results not typical" is **not** sufficient; disclose "generally expected results" unless the featured result is substantiated as representative. Material connections (revenue share, free Pro tier, referral bounties) must be disclosed. Intermediaries that disseminate deceptive endorsements are liable; manipulating rankings (hiding losers) is called out. Source: https://www.ftc.gov/business-guidance/resources/ftcs-endorsement-guides-what-people-are-asking
- **FTC Consumer Reviews and Testimonials Rule (16 CFR 465, effective Oct 2024)**: civil penalties for fake or suppressed testimonials and fake social-proof indicators. Follower counts and ratings must be real. Source: https://www.ftc.gov/business-guidance/resources/consumer-reviews-testimonials-rule-questions-answers
- **FINRA** Rule 2210 / finfluencer sweep binds member firms, relevant only if a broker pays Koine to promote it, and as the standard regulators expect. Source: https://www.finra.org/rules-guidance/guidance/sweep-letters-and-updates/update-social-media-influencers-customer-acquisition-related-information
- **Practical disclosure set**: (1) every strategy page and the marketplace: "Past performance does not indicate future results"; period, fees included, data source, verification tier, and a cohort distribution stat (median return, share of tracked strategies that are negative); (2) a "How creators are paid" line wherever a creator is promoted; (3) a disclosure label on any paid placement; (4) never hide or delist losing strategies from the public dataset; (5) paper tailing labelled "simulated" with the fee model stated; (6) keep the "Sample data" banner regime.

**C: Recommendations.** Kraken first (clearest terms), Alpaca via OAuth read-only second, Coinbase only after partner approval or through an aggregator; never take broker passwords; treat each venue's "third party at your own risk" clause as a required consent screen; ship cohort-level "generally expected results" stats before featuring any individual creator; put a written performance-claims policy in docs/methodology.

---

## Cost table (2026, USD)

| Item | Cost | Source |
|---|---|---|
| Stripe card processing | 2.9% + 30c | stripe.com/pricing |
| Stripe Billing | 0.7% of billing volume | Stripe support |
| Stripe Tax | 0.5% (no-code) or $0.50/txn (API) | stripe.com/tax/pricing |
| Stripe Connect (Express-style) | $2/active account/mo + 0.25% + 25c per payout; 1099 $2.99 fed/$1.49 state | stripe.com/connect/pricing |
| Stripe Managed Payments (MoR) | ~+3.5% | third-party [VERIFY] |
| AWS KMS | $1/key/mo; $0.03 per 10k requests; 20k free/mo (~$1-3/mo for Koine) | aws.amazon.com/kms/pricing |
| OpenBao/Vault transit | $0 licence; one small VM + ops | openbao.org |
| SOC 2 automation platform | $7.5k-15k/yr | observed pricing |
| SOC 2 auditor | Type I $5k-15k; Type II $15k-45k | drata.com |
| Pen test | $5k-15k | same |
| 2FA libs | `pyotp`, `webauthn` (free) | PyPI |
| Secret scanning | gitleaks free; GitHub Secret Protection $19/committer/mo | github.blog |
| Trolley / Tipalti | ~$500/mo minimum / $99-199/mo entry | third-party |

## Could not verify [VERIFY]
1. Exact Coinbase Developer Platform Terms wording on aggregation/sharing.
2. Binance global Terms API clause text.
3. Stripe stablecoin fee (1.5%) and Managed Payments surcharge (3.5%) / GA status.
4. Fly KMS current availability.
5. Coinbase affiliate terms; Polymarket US affiliate payouts.
6. Whether a consented analytics aggregator is a GLBA "financial institution".
7. Illinois money-transmitter statute current form (Money Transmission Modernization Act adoption).
8. Vanta/Drata/Secureframe list prices.
9. Whether the CFPB's August 2026 1033 NPRM has been published in the Federal Register.
