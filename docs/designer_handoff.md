# Koine: frontend handoff for a designer

Snapshot of branch `platform-v2` at `cad8e67` (2026-10-07). Written from the code, not
from the live site. Section 7 says what the code has that a visitor to the deployed
site will not see.

## 1. How the frontend is built

- **Server-rendered Jinja2. There is no SPA.** Templates are in `src/alphamarket/templates/`.
  Every page is a FastAPI route that renders HTML.
- **Charts are inline SVG.** `charts.py` builds the geometry and the `area` macro in
  `_macros.html` draws it. There is no chart library.
- **Client JS is two small files:** `static/app.js` and `static/claim.js`.
  - `app.js`: closes dropdown menus, draws the chart in left to right, shows the
    hover crosshair on the strategy equity chart, copies the invite link, asks
    `confirm()` before destructive forms, and auto-submits the marketplace
    "Show incubating" switch.
  - `claim.js`: the "Sign with my wallet" button on the Hyperliquid claim page.
- **CSP is `script-src 'self'`.** Inline `<style>` and `style=""` are allowed. Inline
  `<script>` is not. New JS goes in a file under `static/`.
- **Same origin only.** There is no CORS. A separate React app on another origin would
  need a proxy or CORS work first. The JSON API is thin (section 4), so a rebuild would
  also need new endpoints. The lowest-risk plan is to restyle the Jinja templates.
- Google Fonts load from `fonts.googleapis.com`. Only that host is preconnected.

## 2. Design tokens (`templates/base.html`)

Brand is **Koine**. Warm off-white, serif headlines, navy for actions, green reserved for
"verified".

| Token | Value | Use |
|---|---|---|
| `--bg` | `#f6f4ef` | page background |
| `--surface` | `#ffffff` | cards, inputs |
| `--line` / `--line2` | `#e4e0d8` / `#cfc9be` | hairlines / card and input borders |
| `--chip` | `#ece9e2` | segmented control, disabled fill |
| `--text` / `--text2` / `--muted` | `#141518` / `#4a4e56` / `#7b8089` | |
| `--brand` | `#1b3556` | navy: h1/h2, primary button, `.card.ink` |
| `--brand2` | `#3b7fb8` | link hover, focus ring |
| `--good` / `--bad` / `--warn` | `#1b7a47` / `#b6363f` / `#9a6a10` | with `--goodbg` `#e8f2ec`, `--badbg` `#f7e8e9`, `--warnbg` `#f6efdf` |
| `--gray` | `#b3b7be` | benchmark lines |

- **Fonts:** Newsreader (serif, headings), IBM Plex Sans (body 15px/1.5), IBM Plex Mono
  (all numerics, labels, tables, badges; tabular-nums).
- **Shape:** 4px radius everywhere, 999px for pills. `main` is max 1200px wide.
  Nav is 64px high. Shadows are minimal.
- **Hard-coded colours outside the tokens:** button hover `#24466f`, avatar `#d9d4c9`,
  the sigil and category palette in `marks.py:10-29`.
- **Not present:** dark mode, favicon, meta description, og tags, print styles.
- **Breakpoints:** `1000px` (grid collapses, desktop nav becomes a "Menu" dropdown) and
  `600px` (16px gutters, filter chips scroll). `prefers-reduced-motion` is honoured.
- **Component classes** are all in `base.html`: `.btn` (+ `secondary`, `danger`, `light`,
  `lg`, `sm`), `.card` (+ `plain`, `ink`, `tint`), `.stats`/`.stat`, `.badge`, `.chip`, `.tag`,
  `.field`, `.toggle`, `.error`, `.notice`, `.seg`, `.subnav`, `details.menu`, `.scard`.
- **Macros** (`_macros.html`): `icon` (24 names), `logo`, `pct`/`usd`/`num`/`qty`/`price`,
  `sparkline`, `verified`, `learn_tabs`, `bar`, `cat`, `area`, `strategy_card`.

## 3. Screens

Access: **Anon** = anyone, **User** = logged in. There is no creator role. A "creator"
is a user who has connected a venue.

| Path | Template | Access | Data |
|---|---|---|---|
| `/` | `index` | Anon | **Live** hero card and top-3 table; rest is copy |
| `/marketplace` | `marketplace` | Anon | **Live** |
| `/s/{slug}` | `strategy` | Anon | **Live**, the main product page |
| `/s/{slug}/tail` | `tail` | User | Live; form |
| `/portfolio` | `portfolio` | User | **Live** |
| `/dashboard` | `dashboard` | User | **Live** |
| `/dashboard/connect?venue=` | `connect` | User | Live; changes per venue |
| `/dashboard/connect/claim/{id}` | `claim` | Owner | Live |
| `/dashboard/connect/snaptrade/{id}` | `snaptrade_accounts` | Owner | Live (venue call) |
| `/dashboard/accounts/{id}/rotate` | `rotate_key` | Owner | Live |
| `/dashboard/security` | `account_security`, `totp_enroll`, `recovery_codes` | User | Live |
| `/login`, `/signup` | `auth` | Anon | Form |
| `/login/2fa` | `login_2fa` | Half-logged-in | Form |
| `/forgot-password`, `/reset-password`, `/verify-email`, `/account/email/confirm` | `password_reset`, `account_notice` | Anon | Form / notice |
| `/creators` | `creators` | Anon | Static copy |
| `/invite` | `invite` | Anon | Stub |
| `/verification` (= `/methodology`), `/security` | `methodology` | Anon | Markdown from `docs/*.md` |
| `/transparency` | `transparency` | Anon | Live counts only |
| `/research` | `research` | Anon | Static, mostly "coming soon" |
| `/feed`, `/u/{handle}` | `feed`, `creator` | **Demo only** | Sample data |

### Screens that need live data to make sense

These look empty or thin with no ingested data. Judge them against the seeded demo
(section 8), not an empty database.

- **Marketplace.** Cards need a return, drawdown, 60-day sparkline, tags and a verification
  state. Each row has `slug, name, display_name, tags[], venue_label, verification_tier,
  total_return, max_drawdown, sharpe, risk_score (1-10), track_days, reconciled, spark`.
  Filters: `?tag= &q= &venue= &asset=(prediction|equities|crypto) &view=(cards|table) &incubating=1`.
- **Strategy page.** The most data-hungry screen. It needs a stats row, which only exists
  after the first nightly job. Contents:
  - header with badges, a 6-stat strip, the equity chart with a drawdown shade, a drawdown
    chart, a monthly-returns table, dated worst periods (only after 90 days), holdings and
    exposure (asset venues), and recent trades or settlements;
  - a paper-tail card whose button changes with state: anon, own (disabled), active, ready.
  Real records have **no** benchmarks and **no** discussion thread. Those are sample-only.
- **Portfolio.** Needs paper allocations: value, P&L, drawdown, a P&L curve (needs 2 or
  more snapshots), risk key-values, an allocations table with Stop, activity, open positions.
- **Dashboard.** One card per connected account with a status badge, a 6-stat strip, a
  nightly reconciliation table (last 5 runs), sync/reconcile/disconnect actions, and a listing
  editor. States to design: connected, importing, sync error, pending, suspended, unclaimed,
  disconnected.
- **Landing.** The hero strategy card has three states: live row, sample row, empty
  placeholder.
- **Transparency.** Live shows five counts only (`ever, losing, median_return, median_dd,
  removed`). The histogram, outcomes and failed register exist only in sample data.

### Designed but never built for live data

Feed (rooms, chat, posting, filters), creator profiles, benchmarks and discussion on
strategy pages, a Transparency histogram, Research guides, working Invite, and the
performance fee and "capacity left" from the design pack. See section 7.

### Error pages do not exist

There is no HTML 404, 403, 429 or 500 page. Those return bare JSON such as
`{"detail":"Not Found"}`, and a CSRF failure returns plain text. Hidden strategies, an
unknown venue, and another user's allocation all produce a JSON 404. **Design these.**

## 4. How sign-in works

Session cookies, not tokens. The same endpoints serve the HTML forms and a JSON API.

### Sign-up and login

1. `POST /auth/signup` (JSON) or `POST /signup-form` (form). Password is 10-128 chars
   and at most 72 bytes. **The user is logged in straight away**, with
   `email_verified=false`. A verification email goes out (link valid 48 hours).
2. `POST /auth/login` (JSON) or `POST /login-form`. If the user has no 2FA, the response
   is `{"id":1,"email":"a@example.com","display_name":"Ann","email_verified":true,
   "totp_enabled":false,"mfa_required":false}` and the cookie is set.
3. If 2FA is on, login returns `mfa_required:true` and a **pending** cookie (10 minutes).
   Then `POST /auth/totp/verify {"code":"123456"}` (an app code or a recovery code)
   completes it. The HTML flow redirects to `/login/2fa`.
4. `POST /auth/logout` returns 204. `GET /auth/me` returns the user, or 401.

### Session cookie

`koine_sid`: `HttpOnly; SameSite=Lax; Path=/; Max-Age=30 days`, `Secure` in prod. The server
stores only a hash. Idle timeout is 24 hours. **The cookie is replaced on every login, signup,
TOTP confirm and step-up**, so the client must keep honouring `Set-Cookie`.

### CSRF

No token. The server checks that `Origin` (or `Referer`) matches `PUBLIC_BASE_URL`, on
POST/PUT/PATCH/DELETE, once a cookie exists. Browsers send `Origin` themselves. A failure
is a plain-text 403. Forms need no hidden token field.

### Two-factor, step-up and what each action needs

- **Enrol:** `POST /auth/totp/enroll` returns `{"secret":"…","provisioning_uri":"otpauth://…"}`.
  `POST /auth/totp/confirm {"code"}` returns `{"enabled":true,"recovery_codes":[10 codes]}`.
  Recovery codes are shown **once**. The QR SVG is HTML-only.
- **Step-up:** `POST /auth/totp/step-up {"code"}` returns 204 and is valid for 10 minutes.
- **Connecting a venue needs all three:** TOTP on, email verified, and a fresh step-up. The
  403 messages are `NEEDS_TOTP`, `NEEDS_VERIFIED_EMAIL`, `NEEDS_STEP_UP`. The connect page
  shows a notice with an inline 6-digit confirm form and disables the submit button.
- **Disconnecting a venue, disabling 2FA, regenerating recovery codes and changing the
  email** need step-up.
- **Rate limits (key ones):** login 10 per minute per IP and 20 per hour per email; signup
  5 per hour per IP; wrong TOTP code 5 per 5 minutes, then a 15-minute lock; password reset
  3 per hour per email; `/api/*` 60 per minute per IP; connect and rotate 10 per hour per user.
  A 429 carries `Retry-After`.

### Password reset, email change, sessions

- `POST /auth/password-reset/request {"email"}` always returns 200 (it never reveals whether
  the account exists). The link is valid 1 hour.
  `POST /auth/password-reset/confirm {"token","password"}` does **not** log the user in. It
  signs out every session.
- `POST /auth/password/change`, `POST /account/email` (session, password and step-up; the link
  goes to the new address), `GET /auth/sessions`, `DELETE /auth/sessions/{id}`,
  `POST /auth/sessions/revoke-all`.
- `GET /verify-email?token=` and `GET /account/email/confirm?token=` **consume the token on
  GET**. A mail scanner that prefetches the link can use it up. Worth knowing when designing
  the "link expired" state.

### Error shapes

`{"detail":"…"}` for most errors. Validation errors are `{"detail":[{"type","loc","msg"}]}`.
Signup returns 409 `Email already registered`. Login failure is 401 `Invalid credentials`,
identical for a wrong password and an unknown email. The HTML forms re-render the page with an
inline `.error` instead.

### Example: curl

```bash
B=http://localhost:8000; J=cookies.jar; O='Origin: http://localhost:8000'

curl -si -c $J -b $J $B/auth/signup -H 'Content-Type: application/json' -H "$O" \
  -d '{"email":"a@example.com","password":"correct horse battery","display_name":"Ann"}'
# 201 {"id":1,"email":"a@example.com","display_name":"Ann","email_verified":false,"totp_enabled":false}

curl -s -c $J -b $J $B/auth/login -H 'Content-Type: application/json' -H "$O" \
  -d '{"email":"a@example.com","password":"correct horse battery"}'

curl -s -b $J $B/auth/me
```

## 5. Other API endpoints

Everything below is JSON and needs the session cookie unless marked anon.

| Method and path | Purpose | Response (abridged) |
|---|---|---|
| `GET /health` (anon) | liveness | `{"status":"ok"}` |
| `GET /api/strategies/{slug}` (anon) | public track record | `{slug, name, venue_label, verification_tier, reconciled, stats{total_return, max_drawdown, sharpe, risk_score, equity_curve[[date,v]], drawdown_curve, monthly_returns[[y,m,r]], …}, incubating, …}`; 404 if hidden |
| `GET /api/strategies/{slug}/signals` (anon) | creator's signals | `{"slug","signals":[…]}` |
| `POST /venues` | connect a venue (needs connect-ready) | 201 `{id, venue, venue_env, status, strategy_id, claimed, claim_code, verification_tier, imported_fills, …}` |
| `GET /venues` | my venue accounts | list |
| `POST /venues/{id}/claim`, `/sync`, `/reconcile`, `/rotate` | account actions | account / reconcile result |
| `DELETE /venues/{id}` | disconnect and erase key (needs step-up) | 204 |
| `PATCH /strategies/{id}` | edit listing | `{id, slug, name, description, style_tags, public, status}` |
| `POST /follows/{slug}`, `DELETE /follows/{slug}`, `GET /follows` | follow | list of `{strategy_slug, strategy_name, created_at}` |
| `GET /api/feed` | followed and tailed activity | `{"items":[{kind:"fill"\|"settlement", at, strategy_slug, ticker, side, …}],"n_following":N}` |
| `POST /allocations` | start a **paper** tail | 201 allocation view; body `{strategy_slug, allocation:"2000.00", drawdown_limit_bps:1500, mode:"paper"}`; live mode returns 403 |
| `GET /allocations`, `GET /allocations/{id}`, `POST /allocations/{id}/stop` | manage tails | `{id, status, allocation, cash, equity, pnl, return, drawdown, positions[], curve[[iso,eq]], paper:true}` |
| `GET /api/portfolio` | totals | `{paper:true, allocations[], n_active, allocated, value, pnl, as_of}` |
| `GET /oauth/{venue}/start`, `/callback` | Alpaca OAuth (PKCE), redirects | 302 / 303 |

Gaps if anyone wants a JSON-driven frontend: no JSON endpoint for the marketplace list or the
dashboard, none for the consent text and its `consent_sha256` (only a hidden field on the HTML
connect page), and `/auth/me` does not say whether a step-up is active.

HTML form posts (they redirect, not JSON): `/login-form`, `/signup-form`, `/logout-form`,
`/dashboard/connect`, `/dashboard/strategy/{id}`, `/dashboard/accounts/{id}/{sync|reconcile|claim|disconnect}`,
`/s/{slug}/tail`, `/s/{slug}/follow`, `/s/{slug}/unfollow`, `/portfolio/allocations/{id}/stop`,
and the `/dashboard/security/*` family.

## 6. Connect flow by venue

The connect page changes with the venue's credential kind: RSA PEM (Kalshi), key and secret
(Binance.US; Kraken and Coinbase when open), report token (IBKR), public address with a later
ownership proof (Polymarket by bio code; Hyperliquid by wallet signature), OAuth (Alpaca) and
aggregator (SnapTrade). Every path shows a consent screen. The consent hash is stored.

| Venue | Connectable by a normal user today |
|---|---|
| Kalshi, Polymarket, Hyperliquid, Binance.US | Yes |
| Kraken, Coinbase, Alpaca, IBKR, SnapTrade | **No.** Closed pending written partner approval. The page shows a notice and a disabled button. |

Koine never accepts a venue username or password, only read-only keys, OAuth grants, report
tokens, aggregator connections or public addresses. Tailing is paper only, with no live orders.

## 7. What is in the code but not visible on the deployed site

First, I could not confirm which site you are looking at. The repo records no deployed URL.
`fly.toml` names an app `alphamarket`, but the founder checklist for the Fly launch is still
unchecked in `docs/implementation_plan.md`. There is also a documented **Vercel demo**
(`DEMO_MODE=true`, no worker). The two look very different. Compare your site to this list.

### Only with `DEMO_MODE=true` (off in production)

- **`/feed`** returns 404 in production, even when logged in. The nav hides its link.
- **`/u/{handle}`** creator profiles return 404 in production.
- **Logged-out `/portfolio`** returns 404 in production (it is not a login redirect).
- **The 11 sample strategies** (`fed-path-both-directions`, `weather-cpi-mean-reversion`,
  `btc-eth-weekly-trend`, `funding-aware-btc-perps`, `semis-swing-long-only` and others) and
  their `/tail` pages.
- **Benchmark lines** (S&P 500, Bitcoin, T-bill) and "vs benchmark" rows, the **discussion
  thread**, the sample **"score"** and **"paper tails"** columns, the **Transparency
  histogram**, outcomes bars and failed register.
- The yellow "Sample data" banner.

### Built, but hidden or empty until data exists

- **Marketplace and landing show "No verified strategies yet"** until the worker's nightly job
  (04:15 UTC) writes a stats row. A fresh deployment looks empty for that reason.
- **Strategies are hidden (404)** when the creator sets them private, when they are
  quarantined or delisted, when ownership is unclaimed (Polymarket, Hyperliquid), or before
  their first stats run. Under 90 verified days they are "incubating" and appear only behind
  the **Show incubating** switch.
- **Dated worst periods** appear only after 90 days.
- **Crypto and equity marks** need price jobs and API keys (Alpaca data key, CoinGecko key).
  Without them holdings show as unpriced.
- **Dashboard states** (importing, sync error, suspended, pending, quarantined, flags) need
  a real account in each state.
- **Auth screens that need a mail server:** email verification, password reset and email
  change all depend on SMTP. With the default `EMAIL_BACKEND=console`, no email is sent.
  The deploy doc's "First deploy" secrets block omits the SMTP settings, so a deployment that
  follows only that block cannot verify emails, and so nobody can connect a venue.
- **`/dashboard/security`, `/login/2fa`, the connect and claim pages** show only after login
  and the right account state.
- **The `/docs` and `/redoc` API pages** are not switched off in `create_app`.

### Shelved, stubbed or placeholder (visible but not working)

- **Settled-trades ticker:** fully built (`ticker` macro, `public.recent_public_settlements`)
  and rendered nowhere.
- **`/invite`:** the form sends nothing, ignores its fields, and the copy says "We send one
  message".
- **`/research`:** six guides marked "coming soon", not links; the checklist boxes are inert.
- **Disabled controls:** the feed filters and Post button, the creator Follow button (the copy
  "launches with the feed" is stale), and the own-strategy "Start a paper tail".
- **Payments and pricing:** text only. There is no Stripe code. Copy says "Billing has not
  launched". Creator royalties are described but not built.
- **Real following has no home page.** Follow works on a strategy page, but the only list of
  what you follow is the `/api/feed` JSON, because `/feed` is 404.
- **Not built at all:** live order mirroring (deliberately never), capacity limits, performance
  fee and high-water mark (removed from scope though the design pack still shows them), social
  chat and rooms, cohort "expected results" stats.

### In the design pack (`design/koine/`) but not in the product

Capacity-left and the capacity/length/risk filters, the pick-a-connected-account tail flow with
fee review, feed rooms and chat, creator profiles with posts and followers, and the
performance-fee language on Tail, Creators, Strategy, Portfolio and Transparency. The pack is
a mock-up of the long-term vision. The product has moved to flat subscriptions, and a test pins
the old fee phrases out of the live pages.

### Copy that disagrees with the product (fix when restyling)

1. `index.html:69` and `marketplace.html:111` talk about a ranked "Koine score". Live rows have
   no score and sort by total return.
2. `creators.html:45` says a page goes live after 90 days of incubation. In fact a page goes
   live after its first nightly run and is only labelled incubating.
3. The landing and creators pages mention a subscription and royalty; pricing says nothing is
   charged yet.
4. Marketing lists crypto spot and stocks as supported. On a fresh production site those
   venues are closed to connection (section 6).
5. `strategy.html:35` prints the raw venue slug (`kalshi`) rather than the label on real
   binary pages.
6. The marketplace empty state shows under the Compare table header even in Browse view, and
   the table columns are decided by the first row only (breaks when live and sample rows mix).
7. The empty hero card hard-codes "you · Kalshi".
8. Footer lists Verification and Transparency twice; real logged-in users get Portfolio in
   the header but not the footer; the mobile menu lacks "Connect a key" and "Invite".
9. A signup validation error shows the raw pydantic message.

### Docs gap worth a look (not design)

`docs/venues/` has terms notes for Alpaca, Binance.US, Coinbase, Hyperliquid, IBKR, Kraken
and SnapTrade, but **none for Kalshi or Polymarket**, which are the two open prediction-market
venues. Project invariant 10 says a venue is connectable only with such a note.

## 8. How to see every state locally

```bash
uv sync --all-groups
docker compose up -d db
uv run alembic upgrade head
# Seeded demo: a dedicated *_demo database, see docs/demo.md
DEMO_MODE=true uv run python scripts/seed_demo.py
DEMO_MODE=true uv run uvicorn alphamarket.main:app --reload
```

The seed makes 8 strategies across six venue profiles (including losers, an incubating record,
a stopped tail and a daily-verified record) and demo logins (`investor@demo.koine`,
`analyst@demo.koine`, password in `docs/demo.md`). Every state in section 3 is then visible.
Seeded users are flagged `is_demo`, and preflight fails if any exist in a production database.

## 9. Suggested review order

1. Tokens and global chrome (`base.html`).
2. Marketplace, then the strategy page.
3. The landing hero card in all three states.
4. The tail flow and the portfolio it lands on.
5. The dashboard with multi-state accounts.
6. The connect variants, gate notices and closed-venue state.
7. Auth, security and notice pages.
8. The missing pieces: HTML 404/403/429/500 pages, mobile treatment for connect and invite,
   and a decision on Feed, creator profiles and the live Transparency charts.
