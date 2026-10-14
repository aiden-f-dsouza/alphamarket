# Hyperliquid fixtures

Every file here was captured on **2026-09-26** from Hyperliquid's **public**
info endpoint (`POST https://api.hyperliquid.xyz/info`, no account or key
exists or is needed) by a one-off recording script, never by tests
(invariant 6). The addresses are public trading accounts, not ours:

- `0x1a13afd75418fd72dfa80da33cbca489e231caec` ("account A"): picked from the
  public leaderboard (`stats-data.hyperliquid.xyz/Mainnet/leaderboard`) for
  moderate activity across perps, a HIP-3 dex (`xyz:`) and spot. Its
  abstraction mode is `unifiedAccount`.
- `0x178d245216beb3d7e3e64dce8494b1efe04a9266` and
  `0x83d65728033855dc74b99e9149a12570c14cc850`: leaderboard accounts with
  varied ledger-update types.
- `0xf649b3c03a43dc9bf4562cd0a3bfe6b1aa03422f`: found through the HLP
  liquidator vault's fills (`liquidation.liquidatedUser`); backstop-liquidated
  on 2026-08-19.

Verbatim (no edits):

| File | Request |
|---|---|
| `userFillsByTime_*.json` | A, `startTime` 1790348521663, `endTime` 1790444910239 (60 fills: perp close/open, HIP-3 closes, spot `@210` buys) |
| `userFunding_*.json` | A, 2026-09-26 13:00-16:00 UTC (15 rows, 5 coins x 3 hours) |
| `userNonFundingLedgerUpdates_*.json` | A, `startTime` 0 (all 39 rows) |
| `clearinghouseState_*.json` / `clearinghouseState_xyz_*.json` | A, first perp dex / `dex: "xyz"` |
| `spotClearinghouseState_*.json` | A |
| `userAbstraction_*.json` | A (`"unifiedAccount"`) |
| `userRole_user_*.json` / `userRole_vault_*.json` | A / the HLP vault `0xdfc2...f303` |
| `userFillsByTime_liquidation_*.json` / `userNonFundingLedgerUpdates_liquidation_*.json` | liquidated account, +-60 s around 1787172675479 |
| `candleSnapshot_BTC_1d_*.json` | BTC, `1d`, 2026-09-20 to 2026-09-26 (the last candle was still open) |
| `fundingHistory_BTC_*.json` | BTC, 2026-09-26 00:00-06:00 UTC |

Trimmed (rows selected, each kept verbatim):

- `userNonFundingLedgerUpdates_variety_*.json`: the first row of every
  distinct (type, token, direction, dex route, fee) shape from the two
  variety accounts; row 8 (`cStakingTransfer`) is from `0x83d6...c850`,
  every other row from `0x178d...9266`.
- `meta_*.json`: first perp dex, universe cut to BTC, ETH, LDO, LINK, ONDO,
  PUMP, XPL plus their margin tables. `meta_xyz_*.json`: `xyz:RKLB` only.
- `spotMeta_*.json`: tokens USDC, PURR, HYPE, UETH, USDE, USDT0, UXPL, USDH
  and pairs `PURR/USDC`, `@107` (HYPE), `@210` (UXPL). Token `MAX` is left
  out on purpose (tests the unknown-token fallback).
- `perpDexs_*.json`: the first dex (`null`) and `xyz` only, big per-asset
  arrays dropped. The live list had 11 dexes.
- `l2Book_BTC_*.json`: 3 levels per side. `allMids_*.json`: 6 coins.
- `vaultDetails_*.json`: HLP, `name`/`vaultAddress`/`leader`/`relationship`.

Docs examples (not captures) used inline in the tests: the spot `@107` sell
fill, `builderFee`, the standard-mode `clearinghouseState` and
`spotClearinghouseState`, `userRole` agent/subAccount shapes, and the
ledger types no public account showed (`accountClassTransfer`,
`internalTransfer`, `subAccountTransfer`, `vault*`, `rewardsClaim`,
`spotGenesis`), from
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
and `.../websocket/subscriptions` (TypeScript types), read 2026-09-26.
**Re-record them from a real account** (ours or a consenting creator's) when
one exists, starting with a withdrawal (fee semantics), a vault withdrawal
and a repaid borrow.

Findings worth knowing:

- `userFillsByTime`, `userFunding` and ledger updates come back oldest first;
  `userFills` comes back newest first.
- Funding rows carry the zero hash; ids must be `time:coin`.
- Fills carry `twapId` and sometimes `cloid`; liquidation fills carry
  `liquidation: {liquidatedUser, markPx, method}`, and a backstop fill's
  price moves the remaining margin to the liquidator through `closedPnl`.
- In unified mode, perp `withdrawable` is 0 and spot `hold` equals the perp
  margin in use: spot totals are the cash.
- USDC prints 6 dp in perp fields and up to 8 dp in spot/transfer fields
  (weiDecimals 8); UETH transfers print 9 dp (weiDecimals 9).

## Leaderboard (copy strategies step 7)

`leaderboard_2026-10-14.json`: one GET of the public leaderboard document
`https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` on **2026-10-14**
(the URL the app's /leaderboard page loads; 38,686,933 bytes, 47,097 rows,
no gzip, served from S3 via CloudFront). Trimmed to 27 rows and
**anonymised**:

- Rows chosen: the 8 best week pnl among trading accounts >= $10k, 6 rows
  that had a `displayName`, the 5 small (< $10k) accounts with the highest
  week roi, the 4 worst week pnl >= $10k, and 4 more with 10-dp account
  values.
- Every `ethAddress` is replaced by a synthetic `0x00..0N` (N = row number,
  1-based); every non-null `displayName` by `placeholder-name-N`. The
  fields stay so tests prove the connector drops them. Every other value
  (`accountValue`, `windowPerformances`, `prize`) is verbatim.

Shape: `{"leaderboardRows": [{ethAddress, accountValue, windowPerformances:
[["day", {pnl, roi, vlm}], ["week", ...], ["month", ...], ["allTime", ...]],
prize, displayName}]}`; all figures are decimal strings (pnl, vlm and
accountValue up to 10 dp, roi up to 10 dp), `prize` an int, `displayName`
null or a string. The document is unordered and carries no rank.
