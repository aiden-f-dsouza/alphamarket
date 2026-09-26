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
