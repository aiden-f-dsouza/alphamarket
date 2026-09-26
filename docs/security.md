# Security and privacy: what we can see, and what we can never do

This is the plain-English version of how Koine handles your Kalshi
connection. It is deliberately short. If anything here stops being true, this
page changes first.

## What you give us

A Kalshi API key with **read-only** scope: a key id and an RSA private key.

## What we check before accepting it

1. We make one authenticated read call to confirm the key works.
2. We ask Kalshi's own key registry (`GET /api_keys`) for the key's scopes.
   If the key has `write` scope, **we reject it** and ask you to create a
   read-only one. We never keep a key that could trade.
3. If Kalshi cannot tell us the scope, we accept the key but mark the
   connection "scope unverified" on your dashboard.

## What we can see with it

Your balance, fills, settlements, and open positions. That is the complete
list. We poll these every few minutes and once nightly for reconciliation.

## What we can never do

- **Place, amend, or cancel orders.** The key has no write scope; Kalshi
  enforces that on their side, not us.
- **Move money.** There is no deposit, withdrawal, or transfer capability in
  the Kalshi API for any key, and Koine has no payment integration at
  all. No money moves through Koine, ever.
- **See your Kalshi login, email, or password.** The API key is not your
  login.

## How the key is stored

- The private key is encrypted at rest with AES (Fernet) using a server-side
  master key held only in the server's environment, never in the code
  repository, never in logs, never in any API response.
- This is not a hardware security module. A full server compromise that
  also captured the master key could decrypt stored keys. What an attacker
  could then do is read your trading history: the same read-only limit
  applies to them.
- **Disconnecting erases the encrypted key immediately.** Your already-
  ingested fills and settlements remain, because the track record was built
  on them and the ledger is append-only, but nothing can be fetched again.

## What is public

- Your **display name** (you choose it), your strategy name, tags, and the
  description you write (labeled *unverified*).
- Platform-computed statistics and charts derived from your fills and
  settlements.
- **Never** your email, your key id, your balance in dollars (only returns
  and drawdowns as percentages, and realized P&L), or individual fills.

You can unlist the public page at any time from the dashboard.

## What "verified" means, exactly

Every number on your page is computed by us from data pulled from Kalshi.
Nightly we recompute your cash balance from stored fills and settlements and
compare it with the balance Kalshi reports. If they disagree by more than
0.1%, the page is hidden until a human looks. Details: [methodology](/methodology).

## Reporting a problem

Email the address on the landing page. Security reports are answered first.

## Browser protections

Beyond the account and key handling above, the app itself is hardened
against the standard web attacks:

- **CSRF (cross-site request forgery).** Every state-changing request
  (connect, sync, reconcile, disconnect, claim, follow, allocate, edit a
  listing, log in or out) is rejected with 403 unless its `Origin` header
  (or, when a browser omits `Origin`, its `Referer`) matches Koine's own
  origin. `SameSite=Lax` on the session cookie is the first layer; this
  Origin/Referer check is the second, independent one, because SameSite
  alone is not something every client is guaranteed to enforce. A request
  with no session cookie (a plain API call, or a future webhook) is not
  checked at all: there is no session to forge into.
- **Content-Security-Policy.** Scripts load only from Koine's own origin
  (`script-src 'self'`); there is no inline `<script>` anywhere in the
  rendered HTML and no inline event-handler attribute (`onclick=`,
  `onchange=`, etc.) — the small amount of client JS (closing a menu,
  drawing in an equity chart, copying the invite link, confirming a
  disconnect) lives in one static file the browser fetches once and
  verifies against this policy. Images, fonts and stylesheets are
  restricted to Koine's own origin plus Google Fonts, the only third-party
  asset any page loads. Styles keep `'unsafe-inline'`, because the templates
  use plain `style="..."` attributes for one-off layout; this is a
  narrower exception than script execution and does not, by itself, let an
  injected `<script>` run. Framing is refused entirely
  (`frame-ancestors 'none'`, and the older `X-Frame-Options: DENY` for
  browsers that only understand that header), forms may only submit back to
  Koine (`form-action 'self'`), and `<base>` cannot be used to redirect
  relative URLs elsewhere (`base-uri 'none'`).
- **Other response headers**, sent on every page: `Strict-Transport-Security`
  once the deployment serves HTTPS-only, so a browser never even tries plain
  HTTP again; `X-Content-Type-Options: nosniff`, so a browser never
  reinterprets a response as a different, more dangerous content type;
  `Referrer-Policy: strict-origin-when-cross-origin`, so a link to another
  site only ever leaks that site's own address, never a page's path or
  query string; and `Permissions-Policy` turning off camera, microphone and
  geolocation, none of which this app uses.

## Edge

Every non-demo deployment sits behind Cloudflare in front of the app:

- **TLS in full (strict) mode**, so the connection from Cloudflare to the
  origin is itself verified, not just encrypted, closing the gap a
  Cloudflare-to-origin man-in-the-middle would otherwise leave.
- **WAF managed rules**, Cloudflare's maintained rule sets for common
  injection and exploit patterns, in front of every request before it
  reaches the app.
- **A rate rule on `/auth/*` and `/api/*`**, so a flood aimed at login,
  signup, or the public JSON API is slowed at the edge, before it ever
  reaches the app's own rate limiter or the database.
- **Caching of public pages** (the marketplace, strategy pages, the trust
  pages), so a spike in readers is served from the edge rather than
  recomputing the same platform-verified numbers on every request.
