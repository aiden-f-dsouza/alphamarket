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

## Credential storage (envelope encryption, rotation)

Every venue secret (a Kalshi private key today; exchange API secrets and
OAuth tokens as those venues arrive) is stored with **envelope encryption**:

1. Each stored credential gets its own freshly generated data key.
2. The secret is encrypted with that data key (Fernet: AES-128-CBC with an
   HMAC-SHA256 integrity check).
3. The data key itself is encrypted ("wrapped") by a master key that never
   leaves a key management service. In production that is **AWS KMS**: to
   unwrap a data key the server must ask KMS, every such request is logged
   by AWS, and access can be revoked in one place. Development and tests
   use a local master key instead; the launch check refuses to start
   production with it.

A copy of our database alone therefore decrypts nothing, and neither does a
copy of the database plus the server's configuration: the master key is
not in either. Each record says which master key version wrapped it.

**Rotation.** We can replace the master key at any time without downtime:
the new key is added, every stored data key is re-wrapped under it (with
KMS the data key is re-wrapped inside KMS and the credential is never
decrypted), and the old key is then retired. Credentials stored before
this scheme existed are re-encrypted into it by the same job.

**Who can read ciphertext.** The database has separate roles for the web
server and the background worker. The web role cannot even select the
encrypted columns; only the worker, the process that talks to venues, can,
so a bug in a web page cannot leak key material it has no permission to
read. (The web server switches to its restricted role once the last
on-demand venue calls, such as "sync now", run as background jobs.)

**Disconnecting** still erases the encrypted key, its data key and any
refresh token immediately.

## Audit log

Security-relevant events are written to an append-only audit log that the
database itself refuses to modify or delete (a trigger rejects every UPDATE
and DELETE, for every database user). It records:

- a key being connected, rejected (because it could trade), disconnected,
  or rotated to a new master key;
- **every decryption of a stored key**, with the request or background job
  that needed it and why (sync, reconcile, scope re-check, rotation). The
  audit row is saved before the key is decrypted, so a key is never used
  without a record of it;
- the result of every read-only check, and any suspension;
- sign-in and two-factor events, and administrative actions.

Each entry carries who acted, the IP address and browser of the request,
and a timestamp. Entries never contain secrets: details are scrubbed
before they are written.

Log files get the same treatment. Every log line and error report passes
through a filter that removes private keys, long random-looking strings,
and values of fields named like secrets, passwords or tokens, and error
responses never echo what you submitted.

## Read-only proof re-checks

Accepting only read-only keys is checked once when you connect and
**again every night**. For each active connection we ask the venue what
the key is allowed to do (for Kalshi, its own key registry). We keep the
evidence from the last check on the connection.

- If the venue confirms the key is read-only, nothing changes.
- If the venue cannot answer that night, the connection keeps working and
  the last confirmed check still stands.
- If the key **has gained trading or withdrawal permission**, we suspend the
  connection immediately: the stored key is erased, syncing stops, the
  event is recorded in the audit log, and you are notified. Your track
  record is untouched. To resume, create a new read-only key and connect it.

## Account security

Holding even read-only keys means your Koine account itself must be hard to
take over. These controls apply to every account:

- **Two-factor authentication is required before any key is connected.**
  Koine uses standard authenticator-app codes (TOTP, RFC 6238: 6 digits,
  30-second steps; the previous and next step are accepted for clock drift).
  The shared secret is stored encrypted with the same vault key as venue
  credentials. A code is never accepted twice. You get 10 single-use recovery
  codes when you turn it on; we store only a hash of each.
- **A fresh code is needed for key changes.** Connecting a venue, rotating or
  disconnecting a key, and turning two-factor off all need a code confirmed
  on the current session within the last 10 minutes.
- **Your email must be confirmed** before you connect a venue. Confirmation
  and password-reset links are single-use, stored only as hashes, and expire
  (48 hours and 1 hour). Asking for a reset link says the same thing whether
  or not the email has an account.
- **Sessions live on the server.** Your browser holds a random session id;
  the database holds only its hash. A session ends after 24 hours idle or 30
  days in total, whichever comes first, and a new id is issued at every login
  and every two-factor confirmation. The security page lists every signed-in
  device and can sign out any of them, or all at once. Resetting your
  password signs out everywhere; changing it signs out every other device.
- **Breached passwords are refused** at signup and reset. We check the
  password against Have I Been Pwned using its k-anonymity range API: only
  the first 5 characters of the password's SHA-1 hash leave our server, and
  the match is made locally. If the service is unreachable the password is
  allowed rather than locking you out.
- **Rate limits** (fixed windows, counted before any password hashing):

  | Action | Counted per | Limit |
  |---|---|---|
  | Log in | IP address / email | 10 per minute / 20 per hour |
  | Sign up | IP address | 5 per hour |
  | Two-factor code | account | 5 wrong codes per 5 minutes, then a 15-minute lock and an email |
  | Password-reset request | email / IP address | 3 per hour / 10 per hour |
  | Connect or claim a venue | account | 10 per hour |
  | Public JSON API (`/api/*`) | IP address | 60 per minute |
  | Follow / paper allocation | account | 60 per hour |

  A fixed window of length W seconds that contains time t starts at
  floor(t / W) × W; a limited request gets HTTP 429 with `Retry-After` set to
  the whole seconds left in that window (at least 1). Example: W = 60 and
  t = 1,000,015 gives a window starting at 999,960 and `Retry-After: 5`.
- **Security emails** go to you when your account is signed in from a new
  network, when two-factor is turned on or off or locks, when a recovery code
  is used, when your password changes, and when a key is connected.

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

## Kraken keys

A Kraken key is an API key plus a private key; create it with only Query
Funds, Query Closed Orders & Trades and Query Ledger Entries. Kraken has no
way to report a key's permissions, so when you connect (and every night) we
send two requests a read-only key must be refused: an order with
`validate=true`, which Kraken checks but never sends to the market (a $1
limit price as a second guard), and a request for your withdrawal methods.
If Kraken answers either with anything but "Permission denied", the key is
rejected, or suspended if it was already connected. We store what each check
returned, never the key. Every request is signed on our server with your
private key and a strictly increasing number, so a copied request cannot be
replayed.

## Hyperliquid: no key, signed ownership

Connecting a Hyperliquid account stores only its public 0x address; there is
no key of any kind to leak, and Hyperliquid's API cannot trade on behalf of a
reader. To stop someone claiming another trader's record, the page stays
hidden until the owner signs a Koine message with the wallet (EIP-191
`personal_sign`). The message names the address and a one-time Koine code
and says it is not a transaction; Koine only verifies the signature and never
asks for a seed phrase, private key or API wallet. API (agent) wallets are
refused because they are not the account.

## Alpaca: connecting by OAuth, and the one request that is not a read

Alpaca accounts connect through Alpaca's own sign-in and approval screen.
Koine never sees your Alpaca password and asks for **no permissions** beyond
Alpaca's read-only default (not `trading`, not `account:write`, not `data`).
A grant that includes a trading or account-write permission is refused
before anything is stored. The access token is encrypted like every other
credential and erased when you disconnect; Alpaca offers no programmatic
revoke, so you can also remove Koine in the Alpaca dashboard.

To prove the grant cannot trade, on a **paper** account only, Koine sends one
order request that is invalid on purpose: zero shares of `KOINEPROBE0`, a
symbol that does not exist. It cannot execute even with trading permission,
and paper accounts hold no real money. A grant without trading permission is
refused before the order is even validated; a grant with it gets a
validation error, and the connection is rejected. Live accounts are never
sent this request: there the permission list Alpaca returns with the grant
is the proof, and a grant that comes back without one is marked unverified
rather than read-only. A grant refused at any step is revoked where the
venue allows it.
