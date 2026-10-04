# Hyperliquid

Status: **ok** (reading public account data needs no permission from anyone;
see the US-person section before any US-facing marketing of Hyperliquid
creators).

Checked: 2026-09-26. Connector: `src/alphamarket/connectors/hyperliquid.py`.
Profile: `registry.PROFILES["hyperliquid"]`.

## Terms-of-service position

Sources:

- Hyperliquid Terms of Use, "Last updated on June 15, 2026", made by
  **Hyperliquid Corp.**, https://app.hyperliquid.xyz/terms (read from the
  app's terms bundle on 2026-09-26).
- API docs, https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
  (info endpoint, rate limits), read 2026-09-26.

What the terms cover. They govern "this website-hosted user interface
('Interface'), available at app.hyperliquid.xyz". Hyperliquid itself is
described as "a decentralized, permissionless, and community-driven
blockchain" that the Company "does not own, control, or operate", and
§1.1 says "The Interface is not the exclusive means of accessing
Hyperliquid."

Third-party read access. No clause addresses it, permits it or forbids it.
Every account's fills, funding, transfers and balances are public chain
state, served without authentication by the public info endpoint
(`POST https://api.hyperliquid.xyz/info`), which Hyperliquid documents for
exactly this use ("fetch information about the exchange and specific
users"). Koine never uses the Interface, never holds a key, and never asks
the trader for one. The only clause that touches automated reads is §3.1.8
("bots, scripts, or other automated methods to interact with the Interface
in ways that exceed reasonable usage, bypass rate limits ..."); it applies
to the Interface, and Koine in any case stays inside the published API
limits through the outbound limiter (`RateBudget` in the profile).

Conclusion: **ok**. Nothing to apply for; `requires_partner_approval=False`.

## The US-person restriction and what it means for Koine

§1.6 of the Terms: "The Interface is not available to 'Restricted Persons.'
For the purposes of these Terms, Restricted Persons include: (a) persons or
entities who reside in, are located in, are incorporated in, or have a
registered office in the United States of America or Ontario, Canada; (b)
... jurisdictions subject to applicable economic and trade sanctions ...;
and (c) citizens of Restricted Territories, regardless of their location.
Restricted Persons are strictly prohibited from accessing or using the
Interface described herein." §3.1.5 also prohibits circumvention (VPNs,
false residency statements).

What that means here:

1. **Reading and displaying is not "using the Interface".** Koine reads
   public chain data through the documented API, the same data any block
   explorer shows. The restriction binds people who use the app; it does
   not make an address's history private.
2. **Koine does not decide who may trade on Hyperliquid and does not check
   creators' residency.** The consent text (below) has the creator confirm
   their own use of Hyperliquid complies with its terms and their local law.
   A creator's page is evidence of what an address did, not a statement
   that the creator was allowed to do it.
3. **Followers are never pointed at Hyperliquid.** Copying is paper only
   (invariant 8), there is no "trade this on Hyperliquid" button or referral
   link, and a Hyperliquid strategy page must carry (integration item, not
   yet in the templates) the line: "Hyperliquid's app
   is not available to US persons; Koine shows this record for information
   and paper copying only." Koine never suggests a VPN or any other way
   around §1.6/§3.1.5.
4. **Our own test account.** docs/implementation_plan.md suggests funding a
   wallet to generate our own fills. If the person doing that is a
   Restricted Person, don't: the connector and the smoke script work
   against any public address, and every fixture here came from third-party
   public addresses.
5. **Before US-facing marketing of Hyperliquid creators**, get counsel to
   confirm point 1 and the notice in point 3 [VERIFY with counsel; not yet
   listed in regulatory_reference.md].

## Consent text (connect screen)

> Koine will read your Hyperliquid account's public history (trades,
> funding, transfers and balances) from Hyperliquid's public API and publish
> performance statistics computed from it. We never ask for a private key,
> seed phrase or API wallet, and we cannot trade or move funds. To prove the
> address is yours, you sign a Koine message with your wallet; signing is
> not a transaction and costs nothing. Your page stays hidden until the
> signature checks out. You confirm that your use of Hyperliquid complies
> with its terms and the laws that apply to you. Hyperliquid only serves
> your most recent 10,000 fills, so older trades cannot be verified
> individually; they are covered by the starting balance we record today.

## Read-only proof

`ReadOnlyProof.NONE`: nothing to prove, because Koine holds no credential,
only the public 0x address. `prove_read_only()` always returns
`read_only=True` and records `userRole` (user, vault, subAccount, agent,
missing) in its details so the nightly check notices if the address changes
kind. `check_connection()` refuses "missing" (no Hyperliquid account) and
"agent" (an API wallet, whose own state is always empty; the error names
the master address to connect instead).

## Ownership proof

Hyperliquid has no profile bio, so the Polymarket claim-code flow does not
fit. Instead the trader signs this exact message with the wallet using
EIP-191 `personal_sign` (`claim_message(address, code)`):

```
Koine ownership claim
Venue: Hyperliquid
Address: <lower-case 0x address>
Code: <the account's Koine claim code>
Signing this message proves you control this address. It is not a transaction: it cannot trade, move funds or approve anything.
```

The claim code is the nonce (one per account, issued by `venues.py`).
`verify_claim(encode_claim_proof(code, signature))` recovers the signer and
accepts it when it is the address itself, the `master` of a sub-account
(`userRole`), or the `leader` of a vault (`vaultDetails`); neither a
sub-account nor a vault has a key of its own. Gaps: smart-contract wallets
(EIP-1271) cannot sign this way; the contract has no `OwnershipProof` value
for a signature yet, so the profile uses `PROFILE_CODE` to route through the
claim flow (contract request).

## What the "credential" exposes

Nothing beyond what is already public. The address alone lets anyone read
the account's entire fill, funding and transfer history and its open
positions. Publishing a Koine page makes that history easier to find and
ties it to the creator's chosen display name; the consent text says so.

## Rate limits

Per IP, shared by every account Koine polls (docs "Rate limits and user
limits", 2026-09-26): 1,200 weight per minute. `l2Book`, `allMids`,
`clearinghouseState`, `orderStatus`, `spotClearinghouseState` and
`exchangeStatus` weigh 2; `userRole` 60; every other info request 20. Extra
weight +1 per 20 items returned by `userFills`, `userFillsByTime`,
`userFunding`, `fundingHistory` (and other listed history types), and +1
per 60 items for `candleSnapshot`; the connector charges whole blocks
(`floor(items / 20)`) through `throttle_after` [VERIFY rounding]. WebSocket
user subscriptions are limited to 10 unique users per IP (relevant to P7).
Budget per sync of one account: fills 20 + up to 100, funding 20 + up to
25, ledger 20, metadata 20 per perp dex traded + 20 for spot, balances about
20 + 2 + 20 + 2 per perp dex (11 dexes on 2026-09-26) + metadata.

## History depth

- Fills: only the **10,000 most recent** per address (`history_depth =
  "10k_fills"`); `userFillsByTime` returns at most 2,000 per response. The
  anchor balance taken at connect covers everything older.
- Funding and non-funding ledger updates: time-paginated, 500 rows per
  response; no retention limit is documented [VERIFY on a long-lived
  account].
- Candles: the most recent 5,000 per interval (13+ years of daily closes).

## Mapping (connector summary)

- Perp fill: `FILL_BASE ±sz` of `"<COIN>-PERP"` (HIP-3: `"dex:COIN-PERP"`)
  at the coin's szDecimals; `REALIZED_PNL closedPnl` in the dex collateral
  (USDC on the first dex); `FEE -fee` in `feeToken` (fee already includes
  `builderFee`; a negative fee is a maker rebate and shows as a positive FEE
  leg).
- Spot fill (`"@107"`, `"PURR/USDC"`, resolved through `spotMeta`):
  `FILL_BASE ±sz` of the base token at its weiDecimals, `FILL_QUOTE ∓sz×px`
  exactly, `FEE` in `feeToken` (the base token on buys).
- Funding: one `FUNDING` event per `(time, coin)`, id `f"{time}:{coin}"`.
- Ledger updates: deposit/withdraw (bridge, with the $1 withdrawal fee as
  FEE), sends and transfers to/from other addresses (TRANSFER_IN/OUT plus
  fee and HYPE gas fee legs), transfers between your own spot and perp
  balances (CONVERT, no legs), vault deposit/withdraw/distribution/
  commission, staking moves and borrow/lend supply/withdraw/borrow/repay
  (TRANSFER_IN/OUT), liquidations (LIQUIDATION with no legs: the liquidation
  fills carry the cash, including a backstop's forfeited margin), unknown
  types (ADJUSTMENT with no legs, surfaced by reconciliation).
- Balances: spot totals plus, in standard mode, each perp dex's
  `accountValue - Σ unrealizedPnl`; positions as `"<COIN>-PERP"` with
  `entry_price`; unified-account and portfolio-margin users use spot totals
  only (the docs say perp dex states are not meaningful for them).

## Streaming (P7)

`HyperliquidFillStream`: public websocket `wss://api.hyperliquid.xyz/ws`,
subscription `{"type":"userFills","user":<address>}`, no credential. Mode
FILLS: each `data.fills` element is the same object REST `userFillsByTime`
returns (docs `WsFill`), normalized by the same `to_fill` and stored once
whichever side sees it first (`tid` is the id). The first frame is a
snapshot of recent fills (`isSnapshot: true`), which closes any gap a
reconnect left; duplicates are skipped. The core pings every 30 s (the
server drops a client silent for 60 s). The envelope and the fill keys were
checked with a one-off public probe on 2026-10-04 (snapshot of 30 fills,
keys identical to the REST rows).

**10 unique users per IP** across websocket user subscriptions (docs "Rate
limits and user limits"): `VenueProfile.stream_users_per_ip = 10`, enforced
by slot leases per `EGRESS_IP`, most-followed creators (active paper
allocations) first; everyone else is polled (docs/worker.md "Streaming").
[VERIFY] the exact error text when the cap is hit (the parser treats any
error frame mentioning "users" as the cap and backs off for the cool-off).

## Known gaps

- Only the latest 10,000 fills are verifiable individually.
- Portfolio-margin accounts: borrowing across HYPE/BTC/USDC/USDT is not
  modelled beyond the borrowLend flows; reconciliation will flag them.
- Borrow/lend interest is folded into the flow amounts (treated as a flow,
  not a return) [VERIFY `interestAmount` on a repaid loan].
- Vault accounts: the outflow of a depositor withdrawal is taken as
  `netWithdrawnUsd + commission` [VERIFY with a vault account].
- Withdrawal fee: `usdc` is taken as the bridged amount with the $1 fee on
  top [VERIFY on our own account].
- HYPE staking rewards (`delegatorRewards`) are not ingested; staked HYPE is
  outside the balance vector.
- Market data lists the first perp dex and spot; HIP-3 dexes are priced by
  their own coin names but not listed by `fetch_instruments`.
- Spot tokens keep Hyperliquid's names (`UBTC`, `UETH`, `UXPL`): Unit-bridged
  tokens are not merged with `BTC`/`ETH`.
