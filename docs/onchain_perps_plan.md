# On-chain perp DEXs as watchable venues: build plan (2026-10-22)

Scope: add on-chain perp DEXs as **watch + discover** venues (a public address
pasted into `/watch`, a venue leaderboard on `/watch/discover`, paper tailing
only). This plan covers watching, not wallet connects: every new profile ships
with `requires_partner_approval=True`, so the connect form stays closed while
the watch path (`watchlist._track`, which never calls `venues.ensure_venue_open`)
works. Offshore CEXs are out of scope. Addresses are the only stored identity,
and owners can opt out.

Inputs:
- `docs/research/2026-10-22-{dydx,gmx,drift,lighter}.md`
- `docs/research/2026-10-22-perp-dex-scout.md`
- Raw responses in
  `/tmp/claude-1000/-home-afd22-koine/92fb559b-5b10-41c2-9455-dff60699a8a7/scratchpad/fixtures/<venue>/`
  (copied to `/home/afd22/koine-research-fixtures/` before the scaffold
  starts, see §2; the "research fixtures dir" below means the copy).

Model: Hyperliquid (`connectors/hyperliquid.py`, its `PROFILES` entry,
`docs/venues/hyperliquid.md`).

Model assignment, the same as previous rounds: Opus plans and handles ledger
money and new methodology. Sonnet writes code that maps venue numbers onto an
existing contract.

---

## 1. Venue decisions

### Build now (wave 1, in parallel after the scaffold)

| # | Venue (profile key) | Verdict | Why | Builder |
|---|---|---|---|---|
| 1 | **Lighter** (`lighter`) | go with caveats; **prod gated on counsel Q1** | Best public leaderboard: 4 windows that map 1:1, sorted server side, no names. Full history from public data: explorer logs back to genesis are the primary source, REST `trades` (newest 3,000, documented as auth-only) an optional live-edge accelerator. Uses EVM `0x` addresses, so it needs no address-kind work. Read-only websocket works from the US. Terms name the API and list "unauthorized access to the API" as a suspension ground, so the build must degrade, not break, if REST is locked (W-L L2). | **Opus**: derived funding **and** derived realized PnL on explorer fills, two fill sources, composite account (several indexes per address) |
| 2 | **Pacifica** (`pacifica`) | go with caveats (terms unread) | Closest to Hyperliquid's `info` API: fills, funding, a balance history with running balance, positions and equity, all public with decimal strings. Leaderboard of about 7k rows. `account_trades` websocket keyed on the same `history_id`. Solana addresses, so it is the venue that proves the shared address-kind scaffold. | **Sonnet** (numbers arrive ready-made; see the hand-computed PnL check in W-P) |
| 3 | **GMX v2, Arbitrum** (`gmx_arbitrum`) | go with caveats; **may slip to wave 2** | Official GraphQL leaderboard. Fills keyed `txHash:logIndex` back to 2023-07. Not geoblocked. But wallet-mode accounts have **no venue balance**, so equity and TWR need a new "synthetic margin account" methodology, USD figures arrive at 30 dp (a rounding rule is unavoidable), funding is claimed in non-stable tokens, and reconcile needs a position-only mode. | **Opus**. A design note (G0) must be accepted before connector code (G1) |

Order of value and risk: Lighter, then Pacifica, then GMX. Lighter and
Pacifica start once the scaffold lands. GMX starts as **G0 only** (an Opus
design note, no code). G1 starts only when G0 is accepted. If G0 is not
accepted by the time Lighter and Pacifica merge, GMX moves to wave 2 with Nado
rather than holding a worktree open; no agent sits idle waiting on it.

`asset_classes=("crypto",)` stays for all three, although Lighter and GMX list
synthetic equities, FX and commodities. Watch strategies are never public, so
the marketplace asset filter never sees these venues while they are watch
only; instruments that are not crypto are marked `copyable=False` (S2).
Revisit `asset_classes` (for example a `"synthetic"` class) if a venue ever
opens for connects.

### Wave 2 (after wave 1 merges)

- **Nado** (`nado`, Ink, built by the former Vertex team). Watch only, because
  no leaderboard exists, so `leaderboard_periods=()`.
  - Data: a complete public archive indexer (matches, events, funding).
  - Terms: the US is listed as **view-only**, the cleanest terms position of
    any venue here.
  - Formats: numbers are x18 fixed-point integers, which the scaffold's
    `money.fixed_to_atoms` (S7) handles. Subaccounts are bytes32 (20-byte
    address plus 12-byte name).
  - Builder: Sonnet, with fixtures from `fixtures/nado/`.
  - Deferred only to cap parallel merges at three. Nothing blocks it.

### Deferred or no-go

| Venue | Decision | Revisit when |
|---|---|---|
| **dYdX v4** | **No-go.** The indexer returns `403 GEOBLOCKED` to US IPs on every per-address endpoint (tested live). Fly `iad` is US egress. The terms bar US-controlled persons and ban circumvention, so a proxy, a non-US region or a self-hosted indexer would each be a breach. There is no leaderboard. The websocket snapshot that got through looks like an enforcement gap, so do not build on it. | dYdX opens perps to US persons |
| **Drift / Velocity** | **No-go.** Relaunched about three weeks ago after a $285M exploit. 5 markets, about $50k a day, 62 weekly traders, no history before 2026-09. The terms expressly cover APIs and ban commercial or third-party-benefit use. | 2027-01: re-check volume, terms and a written permission from Velocity |
| **gTrade, Jupiter Perps, Avantis, Ostium** | **Deferred.** Each position is funded from the wallet, so there is no account equity. gTrade sends JSON floats. Avantis and Ostium have no API, so we would need our own log indexer. | After GMX's synthetic margin account (G0) ships: the same model applies to gTrade and Jupiter |
| Aster, Paradex, edgeX, Extended, ApeX, Orderly, GRVT, Bluefin, Aevo | **No.** Per-account history needs a key or JWT, or the venue shows usernames rather than addresses. | n/a |
| Vertex | Defunct (succeeded by Nado). | n/a |

---

## 2. Shared scaffold (ONE commit, Opus, before any venue agent starts)

Branch `onchain/scaffold` off `main` after the current fix branch
(`fix/discover-start-missing-venue`: `worker.py` +
`tests/test_discover_refresh.py`) merges. `WATCH_VENUES = "hyperliquid,polymarket"`
is already on `main` (`73fbf3b`, PR #40). Venue worktrees branch from the
scaffold's merge commit. Everything below is venue-agnostic. No venue module is
written here.

Before the first agent starts, copy the research fixtures dir to a stable
path outside the repo, `/home/afd22/koine-research-fixtures/<venue>/` (raw
and not anonymised, so never committed; a gitignored in-repo copy would not
reach the worktrees). Agents read from there and anonymise into
`tests/fixtures/<venue>/`. "Research fixtures dir" below means that path.

### S1. Address kinds: new pure module `src/alphamarket/connectors/addresses.py`

```python
class AddressKind(StrEnum):
    EVM = "evm"        # 0x + 40 hex, stored lowercased
    SOLANA = "solana"  # base58, 32-byte ed25519 public key, case-sensitive, stored as given

class InvalidAddress(ValueError): ...      # str(exc) is user-facing copy

def normalize(kind: AddressKind, raw: str) -> str
    # EVM: strip, lower, ^0x[0-9a-f]{40}$.
    # SOLANA: strip only; base58 alphabet; b58decode -> exactly 32 bytes.
def is_valid(kind: AddressKind, address: str) -> bool   # already-normalized form
def short(address: str) -> str                           # moved from watchlist.short_address (0x12ab…89cd / 9GN1…AxUh)
def evm_checksum(address: str) -> str                    # EIP-55 via eth_utils.to_checksum_address (GMX GraphQL)
def b58decode(value: str) -> bytes                       # ~20 lines, no new package
def verify_ed25519(address: str, message: str, signature: str) -> bool
    # signature as base58, base64 or 0x-hex of 64 bytes; cryptography's Ed25519PublicKey;
    # message is the exact UTF-8 claim text (Phantom/Solflare signMessage signs raw bytes)
HINT: dict[AddressKind, str]           # "Enter a 0x address (42 characters)" / "Enter a Solana address (32-44 characters, case-sensitive)"
INPUT_PATTERN: dict[AddressKind, str]  # HTML pattern + maxlength per kind
SIGN_HELP: dict[AddressKind, str]      # "personal_sign / EIP-191" vs "Sign Message in Phantom/Solflare, paste the signature"
```

Dependencies: `cryptography` is already a direct dependency. `eth-utils` and
`eth-hash` (with `pycryptodome`) are in `uv.lock` only transitively through
`eth-account`. Because `addresses.py` imports `eth_utils` directly, the
scaffold adds `eth-utils` to `pyproject.toml` `[project.dependencies]` (pinned
to the locked version's minor) rather than relying on the transitive pin.

Tests in `tests/test_addresses.py`:
- Known EIP-55 vectors (from the EIP).
- A base58 round trip on the Pacifica fixture address.
- 31-byte, 33-byte and non-alphabet (`0`, `O`, `I`, `l`) rejections.
- Case preserved for SOLANA.
- ed25519 verification against a key generated in the test, with the signature
  in all three encodings.
- A signature over a different message fails.

No column is widened. `venue_accounts.key_id`, `watch_optouts.address` and
`leaderboard_snapshots.rows[].address` already allow 120 characters. The
widest address in this plan is Nado's 66-character bytes32 hex. Unique indexes
are case-sensitive on both Postgres and SQLite, which Solana needs.

### S2. `connectors/base.py` contract additions (all with defaults; no existing profile changes)

- `VenueProfile.address_kind: AddressKind = AddressKind.EVM`. Read only when
  `credential is CredentialKind.ADDRESS`.
- `LeaderboardEntry.address`: change the docstring to "normalized for the
  venue's `address_kind` (EVM lowercased, Solana as given)". Connectors
  normalize before building the entry.
- Venue privacy signal (Pacifica `privacy_mode`), on **`MarketDataSource`**,
  not `VenueConnector`: the signal is a leaderboard attribute, venue-wide, one
  fetch for every address. Two hooks, both defaulting to None (not offered):
  - `private_addresses(self) -> frozenset[str] | None`: the normalized
    addresses the **last `fetch_leaderboards` call on this instance** saw with
    the flag on (a connector collects them while dropping those rows). No
    extra request. None before a fetch or when the venue has no flag.
  - `owner_private(self, address: str) -> bool | None`: a one-off check for a
    single address (Pacifica: one leaderboard download). Used only at watch
    time (`_track`), never per sync. True means treat the address as opted
    out for as long as the flag is on; None means unknown (for example, an
    address absent from the board).
- `NormalizedInstrument.copyable: bool = True`. False means paper copying
  skips the instrument with skip reason `not_copyable`, while signals are
  still recorded. It is for synthetic US equities and ETFs (Lighter's AAPL,
  SPY and pre-IPO names; GMX's SPY, QQQ and SPCX), FX and commodities, until
  counsel answers Q3. Binary up/down options stay rejected at ingest as
  `InstrumentType.OPTION`.
- `FundingRate.short_rate: str | None = None`. `rate` stays "positive means
  longs pay". `short_rate` is what shorts pay (positive means shorts pay) when
  the venue is asymmetric (GMX adaptive funding plus borrowing). None keeps
  today's rule: shorts receive `rate`.
- `RateBudget` docstring: a venue with two hosts (an API and an explorer)
  declares one budget per host and gives the other host's endpoints **cost 0**
  in each budget. `VenueLimiter.acquire` already skips `cost <= 0`
  (`venue_limits.py:491`), so no code change; add one regression pin that a
  cost-0 charge neither blocks nor consumes tokens, since venues now rely on
  it.
- `VenueProfile.reconcile_mode: ReconcileMode = ReconcileMode.BALANCES`, a new
  enum `BALANCES | POSITIONS`. Today `reconcile.reconcilable()` returns
  `profile.balance_observable`, `reconcile_account` raises
  `BalanceUnavailable` when it is False, and the RECONCILE job skips the
  account, so a GMX position reconcile would never run. With
  `balance_observable=False` and `reconcile_mode=POSITIONS`:
  - `reconcilable()` returns True.
  - `reconcile_account` compares only the perp position vector (signed size
    per instrument, from `fetch_balances` perp entries) against the ledger,
    within the venue's documented quantity tolerance (a new
    `VenueProfile.position_tolerance_atoms: int = 0`), and skips cash and the
    running-balance check. Quarantine works as today on a mismatch.
  - `venues.verification_tier` returns a new tier `positions_verified`
    ("positions verified nightly; cash is reconstructed, not checked against
    the venue"), with copy on the public page and in methodology next to
    `fills_only`.
  - Tests: a stub profile in POSITIONS mode reconciles a matching vector,
    quarantines a mismatched one, never reads cash, and gets the new tier.
  The scaffold makes this change so GMX does not stall on it.

### S3. `watchlist.py`

- `normalize_address(venue, raw)` and `normalize_optout_address(venue, raw)`
  call `addresses.normalize(get_profile(venue).address_kind, raw)`. Map
  `InvalidAddress` to a 422 with `str(exc)`. Delete `ADDRESS_RE` (tests import
  nothing from it; grep confirmed).
- `short_address` becomes a re-export of `addresses.short` (callers:
  `watch_pages.py`, `discover.py`, `demo.py`, `seed_demo.py`).
- `_track`: inside `verify_and_anchor`, after `_verify_new_credential`, build
  the venue's market data (throttled) and, if
  `market_data.owner_private(address) is True`, raise 422 with new copy
  `VENUE_PRIVATE`: "The owner of this address has turned on privacy on
  {venue}." Nothing is stored. Only venues that override the hook pay the
  extra call, and only once per new watch.
- New `remove_private_watch(db, account) -> tuple[int, int]`. It runs the
  removal half of `perform_optout` (delete every `Watch`, stop ACTIVE tails,
  audit) **without** writing `watch_optouts`, because the venue flag can be
  turned off again and `watch_optouts` is append-only. Factor the shared body
  out of `perform_optout` so the two cannot drift.
- New `AuditAction.WATCH_VENUE_PRIVATE = "watch.venue_private"` in `audit.py`.

### S4. Lowercasing sites that become kind-aware

Every site that lowercases an address (grep `\.lower()` and `func.lower` over
`src/` and `scripts/`, 2026-10-22):

| Site | Today | Change |
|---|---|---|
| `watchlist.py:135` `normalize_address`, `:579` `normalize_optout_address` | `strip().lower()` + `ADDRESS_RE` | `addresses.normalize(kind, raw)` (S3) |
| `discover.snapshot_row(entry)` (`discover.py:110-115`) | `entry.address.lower()` | **signature becomes `snapshot_row(venue, entry)`** and uses `addresses.normalize(get_profile(venue).address_kind, entry.address)`. A row that fails normalization is dropped and counted in the job log, never stored. Callers to update: `discover.refresh` (`discover.py:220`), `demo.py:1513`, `scripts/seed_demo.py:2346`. |
| `discover._listed_slugs` (`discover.py:346`) | `func.lower(VenueAccount.key_id).in_(addresses)` | `VenueAccount.key_id.in_(addresses)` |
| `discover.py:353` | `account.key_id.lower()` | `account.key_id` |
| `watchlist.py:166` (claimed creator lookup) | `func.lower(VenueAccount.key_id) == address` | `VenueAccount.key_id == address` |
| `jobs.py:388` (opted-out watch filter) | `WatchOptout.address == func.lower(VenueAccount.key_id)` | `WatchOptout.address == VenueAccount.key_id` |
| `venues.py:486` (connect) | `key_id.lower()` for ADDRESS venues | `addresses.normalize(profile.address_kind, key_id)` |

Dropping `func.lower` is safe only if every stored EVM `key_id` and
`watch_optouts.address` is already lowercase (they are written through the
two normalizers above). The scaffold verifies that with a one-off query on
prod (read-only, main session) before merging. If any mixed-case EVM row
exists, keep `func.lower` for EVM venues instead and branch on the kind.

### S5. Venue privacy sweep, in the discover refresh

`account.json` carries no privacy flag (Pacifica fixture), so the only signal
is the ~7k-row leaderboard. Downloading it per watched account per sync would
spend Pacifica's whole credit budget at `watch_max_tracked=500`. So the sweep
runs **once per `watch_discover` refresh**, from the board the job already
fetched:
- In `discover.refresh`, after `fetch_leaderboards` succeeds for a venue, read
  `source.private_addresses()`. If it is not None, find that venue's
  `is_watch` accounts whose `key_id` is in the set and call
  `watchlist.remove_private_watch` for each.
- Sources that do not override the hook return None and cost nothing.
- `owner_private(address)` is used only at `_track` (S3).
- Limit, stated in the venue doc: an address absent from the leaderboard
  cannot be checked at all; its privacy flag is invisible to Koine, and the
  signed opt-out is its only route.
- Test with a stub market data source: a private watched address is removed
  (no `watch_optouts` row, audit row written); a non-private one is kept; a
  source returning None triggers no query.

### S6. Templates (no new client JS beyond a kind switch)

- `watch.html:41` **and** `watch_optout.html:59` both hardcode
  `pattern="0[xX][0-9a-fA-F]{40}" maxlength="42"` on the address input. Both
  drop the pattern and use `maxlength=66`. The placeholder and hint follow the
  selected venue: render both hints and show one with CSS `:has()` on the
  venue `<select>`. Validation stays on the server.
- `watch_optout.html`: the signing copy comes from `addresses.SIGN_HELP[kind]`.
  `claim.js` engages only when `data-claim-kind="evm"` (window.ethereum).
  Solana proof is **paste the signature** in v1, so there is no new wallet JS
  and CLAUDE.md's "only client JS" list stays true. Replace the venue list in
  the intro paragraph ("public Hyperliquid or Polymarket address") with the
  open venues' display names.
- The `/watch` empty state and the discover venue tabs already iterate
  `open_venues()`, so they need no change.

### S7. Migration `0019_onchain_perps_scaffold` (next after head `0018`)

- `funding_rates.short_rate VARCHAR(64) NULL`
- `instruments.copyable BOOLEAN NOT NULL DEFAULT TRUE` (server_default `true`)
- Neither table is append-only, so no trigger changes. Preflight's
  append-only set is unchanged.
- Model fields: `FundingRateRow.short_rate`, `Instrument.copyable`.
  `ingest`'s instrument upsert writes `copyable` from `NormalizedInstrument`.
- `prices._refresh_funding` stores `short_rate`.
- `paper_lifecycle.funding_payment_micros(qty, mark, rate, short_rate=None)`:
  - long (q > 0): `-q x mark x rate`
  - short (q < 0) with `short_rate` set: `q x mark x short_rate`, which is
    negative when shorts pay
  - short with `short_rate` None: `-q x mark x rate` (today's formula)
  - each floored to the micro, as today.

  Hand-computed tests, for example: short 2 ETH, mark 2500, rate 0.0001,
  short_rate 0.00003 gives payment -150,000 micros.
- `paper.py`: new skip reason `NOT_COPYABLE = "not_copyable"`, checked before
  sizing.
- `docs/methodology.md`: "Paper copying v2: funding" gains the asymmetric rule;
  "Paper copying" lists `not_copyable`.
- New money helper in `money.py`:
  `fixed_to_atoms(raw: str, scale: int, decimals: int, *, rounding: str | None = None) -> int`.
  It takes an exact integer string at 10^scale (GMX 30-decimal USD, Nado x18)
  and returns atoms at `decimals` (12 or fewer).
  - Default (`rounding=None`): it **raises** when the value is not exactly
    representable, like `to_atoms`. Nado and every other caller use this.
  - `rounding=ROUND_HALF_EVEN` (or `ROUND_DOWN`) quantizes instead. Only the
    GMX connector passes it, at the documented sites in G0 item 4, because
    GMX USD figures are 30-dp values that are almost never exact at 6 dp
    (fixture `basePnlUsd = 51033091798973216164946606156791229` is
    51,033.091798973216…). A test greps that no other module passes
    `rounding=`.
  - Unit tests use hand-computed values, including that fixture value
    (half-even at 6 dp gives 51,033.091799 = 51,033,091,799 micros) and a
    half-way tie. A separate
  `fixed_to_decimal_str(raw: str, scale: int) -> str` gives the exact decimal
  for display figures, which then go through `leaderboard_figure` and nothing
  else.

### S8. Registry anchors, so the three branches merge cleanly

In `connectors/registry.py`, add empty marked blocks, one per venue, in each
of these:
- `PROFILES`
- `_BUILDERS`
- `_MARKET_DATA_BUILDERS`
- the `CASH_AT_PAR` union (`# ---- venue-lighter ----`,
  `# ---- venue-pacifica ----`, `# ---- venue-gmx_arbitrum ----`,
  `# ---- venue-nado ----`)

Venue branches write only inside their own block. Do the same in `demo.py`
(discover sample venues) and `scripts/seed_demo.py` (watch seeds). In
`demo._discover_address(venue, i)`, the scaffold makes synthetic addresses
kind-aware: Solana gets base58 `De30…`, still obviously synthetic.

### S9. Preflight

`scripts/preflight.py` refuses any venue in `WATCH_VENUES` that:
- has no existing `tos_doc`, or
- has a `tos_doc` whose "Terms-of-service position" section contains the
  marker `Position: UNREAD` or `Position: PENDING COUNSEL`.

Pacifica uses `UNREAD` until its terms are read (see W-P0). Lighter ships
with `PENDING COUNSEL` until counsel Q1 or a written Lighter answer is on
file (W-L exit). Test both markers.

### Scaffold exit

- `uv run pytest` is green, and ruff is clean.
- No behaviour change for Hyperliquid or Polymarket (existing watch, discover
  and opt-out tests untouched and passing).
- `graphify update .`, then `graphify affected "normalize_address"` and
  `graphify affected "snapshot_row"` list only updated callers.
- A Solana round-trip test (a stub SOLANA-kind profile registered in the
  test): a mixed-case base58 address survives
  `discover.snapshot_row(venue, entry)` → `LeaderboardSnapshot.rows` →
  `/watch/discover` → `add_watch` → `watch_account` lookup with its case
  intact. Today `test_discover_view.py:62-71` pins lowercasing for EVM only;
  that test stays.

### Scaffold status (2026-10-22, branch `onchain-perps`)

Landed: S1-S9 as written, with these deviations (venue agents read them):

- **Synthetic Solana addresses start `De3o`, not `De30`**: base58 has no `0`
  (nor `O`, `I`, `l`). Use `demo.synthetic_solana_address(tag)` for a
  well-formed 44-character one; fixtures replace real Solana addresses the
  same way.
- `discover.select_rows(venue, entries, blocked, *, dropped=None)` takes the
  venue too (it calls `snapshot_row(venue, entry)`). The refresh job log
  gains `(venue, "invalid_addresses")` and `(venue, "private_removed")` counts
  only when non-zero. Existing discover tests changed only at their
  `snapshot_row` / `select_rows` call sites (the venue argument).
- `discover.sweep_private(db, venue, source)` is the S5 sweep; it runs after
  the venue's snapshots commit, in its own try block.
- Preflight (S9) reads `profile.tos_doc`, else `docs/venues/<venue>.md`:
  Polymarket's doc exists but is deliberately not on its profile (no consent
  paragraph on its connect screen), and it is in prod `WATCH_VENUES`.
- The S1 test uses public Solana program ids and keys generated in the test
  rather than the Pacifica fixture address (a real, privacy-flagged trader).
- `NormalizedInstrument.copyable` follows the venue's latest description on
  every upsert (like lot sizes): a connector must set it the same way on its
  fill instruments and in `fetch_instruments`.
- Registry, demo and seed blocks have start and end markers
  (`# ---- venue-<v> ----` / `# ---- end venue-<v> ----`); write inside them.
- **Still open (main session, before merge):** the S4 read-only prod check
  that every stored EVM `venue_accounts.key_id` (ADDRESS venues) and
  `watch_optouts.address` is already lowercase. The scaffold dropped
  `func.lower` on the strength of the two normalizers that wrote them.

---

## 3. Venue workstreams (parallel, one git worktree each)

Common rules for every venue agent:

- **Setup:** `git worktree add ../koine-<venue> -b onchain/<venue> <scaffold-merge-sha>`.
  Run `uv sync --all-groups` inside the worktree.
- **May create or edit:**
  - `src/alphamarket/connectors/<venue>.py`
  - its own `# ---- venue-<venue> ----` blocks in `registry.py`, `demo.py` and
    `scripts/seed_demo.py`
  - `tests/test_<venue>_*.py` and `tests/fixtures/<venue>/` (with `README.md`
    provenance like `tests/fixtures/hyperliquid/README.md`)
  - `docs/venues/<venue>.md`
  - `scripts/<venue>_smoke.py`
- **Do NOT touch:**
  - core: `connectors/base.py`, `connectors/addresses.py`, other connectors,
    `models.py`, `alembic/`, `money.py`, `config.py`
  - pipeline: `watchlist.py`, `discover.py`, `stream.py`, `paper.py`,
    `paper_lifecycle.py`, `paper_book.py`, `prices.py`, `valuation.py`,
    `valuation_db.py`, `ledger.py`, `reconcile.py`, `ingest.py`, `jobs.py`,
    `worker.py`, `venue_limits.py`
  - web: `templates/`, `static/`
  - deploy and project docs: `fly.toml`, `WATCHABLE_VENUES`, `BUILD_PLAN.md`,
    `CLAUDE.md`, `docs/methodology.md`, `docs/security.md`
  - other venues' registry, demo and seed blocks

  If a shared change looks necessary, **stop and report** what and why. Do not
  make it. Methodology text the venue needs goes in a "Methodology notes
  (for docs/methodology.md)" section of the venue doc; the main session merges
  it.
- **Fixtures:** start from the research fixtures dir.
  - Trim to the rows the tests need; keep rows verbatim.
  - Replace every address with a synthetic one of the same kind
    (`0x00…0N` / base58 `De30…N`) as the Hyperliquid leaderboard fixture does.
  - Strip usernames, names and any other identity field.
  - Name files `<endpoint>_<YYYY-MM-DD>.json`.
  - Re-record anything missing with `scripts/record_fixture.py` or the smoke
    script, never in tests.
- **Tests:** respx only, no live calls (invariant 6). Each formula (fee from
  rate, derived funding, PnL split, fixed-point conversion, leaderboard ROI
  scaling) gets a hand-computed expected value with the arithmetic in a
  comment. Run the whole suite before handing back.
- **Money:** ledger values only through `money.to_atoms`, `to_micros` and
  `fixed_to_atoms`, never floats. JSON with float numbers is parsed with
  `json.loads(..., parse_float=Decimal)`. Leaderboard display figures go
  through `leaderboard_figure` and never reach the ledger.
- **The profile** always has (required dataclass fields included, so the
  registry blocks merge the same way; copy Hyperliquid's comments):
  - `credential=ADDRESS`, `secret_label=None`, `readonly_proof=NONE`
  - `key_label="Wallet address"`; `key_help`: one paragraph in Hyperliquid's
    shape ("The <kind> address of your <Venue> account… history is public…
    signing is not a transaction and cannot move funds"); Solana venues say
    "Solana address" and "sign the message in your wallet"
  - `balance_observable`: True for Lighter and Pacifica, False for GMX
  - `tick_micros=1`, `contract_step_micros=1`, `fee_rounding_micros=1` (the
    binary-era fields; real steps are per instrument)
  - `ownership=SIGNED_MESSAGE`, `instrument=ASSET`
  - `paper_fee_model=BPS_OF_NOTIONAL`, `asset_classes=("crypto",)` (see §1)
  - `requires_partner_approval=True` (watch only; no connect)
  - `tos_doc="docs/venues/<venue>.md"`, `envs=("prod",)`

  `fee_rate_bps` is the base taker rate rounded **up** to whole bps.
- **The venue doc** follows `docs/venues/hyperliquid.md` section for section:
  - Terms-of-service position (quotes, date read, URL)
  - The US-person restriction and what it means for Koine
  - Consent text (kept for a future connect; invariant 10)
  - Read-only proof
  - Ownership proof (the claim message and signature scheme)
  - Rate limits
  - History depth
  - Mapping
  - Streaming
  - Watchlist (third-party addresses)
  - Leaderboard (watch discover)
  - Known gaps
  - Methodology notes
- **The smoke script** `scripts/<venue>_smoke.py` makes read-only public calls
  for one address given on the command line. It prints counts and the
  reconcile result, never addresses of other people. It is also the
  **Fly `iad` egress check**:
  `fly ssh console -C "python scripts/<venue>_smoke.py <addr>"`.
- **Demo:** in its demo block, one synthetic leaderboard per period for
  `/watch/discover` (if the venue has one) and one watched synthetic address
  in the seed, written through the real money path like the Hyperliquid
  demo watch.

### W-L. Lighter (`connectors/lighter.py`). Opus.

Hosts:
- API: `https://mainnet.zklighter.elliot.ai/api/v1`
- Explorer: `https://explorer.elliot.ai/api`
- Websocket: `wss://mainnet.zklighter.elliot.ai/stream?readonly=true` (the
  plain URL is refused from the US with code 20558)

Profile:
- `quote_asset="USDC"`, `transfers_observable=True`, `history_depth="all"`
- `fee_rate_bps=3` (Premium taker 2.8 bps, rounded up)
- `leaderboard_periods=("day","week","month","all")`
- `stream_mode=NUDGE` (see L5), `stream_users_per_ip=200`
  (255 connections per IP, minus headroom)
- `rate_budgets`:
  - API: IP-scoped, 60 per minute, explorer endpoints cost 0
  - explorer: IP-scoped, 90 weight per minute, cost 2 per logs page, API
    endpoints cost 0

L1. **Composite account.**
- `key_id` is the lowercased L1 address. Each sync lists the address's
  indexes (`accountsByL1Address`): the master and every sub-account.
- One opaque cursor holds a cursor per index for each stream. New indexes are
  discovered on every sync.
- Transfers between the address's own indexes are internal and emit **no**
  legs. A transfer to any other index is `TRANSFER_OUT` or `TRANSFER_IN`.
- Pool-share and staking events (LLP, LIT staking, public pools) become
  `UnsupportedActivity`, which flags the account.
- **Decide and document:** whether holdings in those pools are excluded from
  reconcile, or whether such addresses are refused at watch time.

L2. **Fills: explorer primary, REST optional, one id.**
- Lighter's docs mark REST `trades` and `pnl` as auth-only, and its terms list
  "unauthorized access to the API" as a suspension ground. So the **explorer
  is the primary and complete fills source**: explorer `logs` (Trade,
  TradeWithFunding, LiquidationTrade, Deleverage) back to genesis, for
  backfill and for every sync.
- REST `trades` (newest 3,000 per index) is an **optional live-edge
  accelerator**, behind a connector flag that defaults on only if counsel Q1
  clears it. On a 401/403 the connector logs once, turns the accelerator off
  for that process, and carries on from the explorer. The sync must produce
  identical legs with the accelerator on or off (a test runs both).
- `venue_fill_id` is `tx_hash` on both sources. If a fixture shows several
  trades under one hash, it becomes `tx_hash:<ordinal>` [VERIFY].
- **Do not filter explorer rows on `status == "executed"`** without
  verifying: the explorer row for a trade REST lists shows
  `status: "nothing_to_execute"` (`explorer_logs_all_page0.json`, hash
  `0000002030…`). Pin which statuses carry real trades from the fixture
  pairs (REST row ↔ explorer row) and test it.
- Backfill walks the explorer newest-first and dedupes on hash. Offsets shift
  during a walk: expect overlap, assert no gaps.
- Fee = `price x size x rate / 1e6` from the integer rate, in Decimal. Choose
  the rounding to USDC's 6 dp, then confirm it against `total_asset_value`
  drift [VERIFY]. Keep `trade_id` as metadata.
- Liquidation and deleverage trades are fills, liquidity "taker".
- **Realized PnL is derived on explorer fills.** REST rows carry
  `ask_account_pnl` / `bid_account_pnl`; explorer pubdata carries only
  `price`, `size`, `is_taker_ask`, fee rates and account indexes. So the
  connector replays an **average entry price per (index, market)** the way
  Lighter does (average cost: an increase re-averages the entry, a decrease
  realizes `(exit - entry) x closed size` with the sign of the position, a
  flip closes then opens at the fill price), and books the REALIZED_PNL leg
  from that replay, rounded to USDC 6 dp under the same rule as the fee.
  - The replay state lives in the opaque cursor (per index, per market:
    signed size, average entry) and starts from genesis when the cursor is
    None; a restarted backfill must reproduce identical legs (test).
  - Check against REST: `taker_entry_quote_before` (and the maker
    equivalent) on REST fixture rows gives Lighter's own entry before the
    fill; the replayed entry must match it, and the replayed PnL must match
    `*_account_pnl`, on every overlapping fixture row (test, exact or within
    a documented 1-micro rounding).
  - A hand-computed test: open 2 @ 100, add 1 @ 130 (entry 110), close 1.5 @
    120 (realized (120 - 110) x 1.5 = +15.00, long 1.5 @ 110 left), then sell
    2.5 @ 90 (closes 1.5: (90 - 110) x 1.5 = -30.00; opens short 1 @ 90).
  - A methodology note in the venue doc: realized PnL on Lighter is
    reconstructed by Koine from public trades, average cost, and checked
    against the venue's own figure where the venue publishes it.

L3. **Funding is derived.** With realized PnL (L2) these are the two computed
ledger streams on any venue, so each needs its own methodology entry.
- First establish, from the `TradeWithFunding.funding_rate_prefix_sum` fixtures,
  whether Lighter settles funding lazily through prefix sums (likely). If it
  does, the derived payment for an index and market is
  `-position x (prefix_now - prefix_at_last_settlement)`, booked when the
  venue settles it.
- Otherwise use hourly `fundings` x the position held at each hour.
- `venue_ref = funding:{index}:{market_id}:{settlement key}`.
- Cross-check: the sum per market equals `positions[].total_funding_paid_out`.
- Hand-computed tests for the sign, the rounding and one multi-hour position.

L4. **Balances.** `fetch_balances` sums the indexes:
- USDC excluding uPnL = collateral
- `<SYMBOL>-PERP` entries with `avg_entry_price`
- spot `assets[]`
- `equity_usd_micros = total_asset_value`

`/api/v1/pnl` is a reconcile cross-check only and is never stored.

L5. **Stream: `NUDGE`.**
- `account_all_trades/{index}`, one subscribe frame per index; a frame queues
  a sync, and the explorer path (L2) books the legs.
- Not `FILLS`: `ws_samples.json` shows REST-shaped frames, but the legs depend
  on replay state (derived realized PnL and funding, composite-account
  transfer rules) that the stream process does not hold. Revisit only if
  the stream can share the connector's cursor state.

L6. **Market data (`LighterMarketData`).**
- Instruments: `orderBookDetails` (tick = 10^-price_decimals, step =
  10^-size_decimals).
- `copyable=False` for equity, ETF, FX, commodity and pre-IPO symbols, by
  `market kind` or category [VERIFY field].
- UPDOWN binaries are `OPTION`, so they are rejected.
- Quotes: `orderBookOrders?limit=1` (check its weight). Funding history:
  `fundings` 1h, written as `FundingRate` (symmetric). Daily closes: candles.

L7. **Leaderboard.**
- `pnlLeaderboard`, for each window: sort by pnl, top pages, then sort by roi,
  top pages.
- `limit=100`, plus enough pages to reach `2 x watch_discover_rows` after the
  account-value floor.
- `roi` is a percent, so divide by 100. Its denominator is unknown and is
  **not** `pnl / account_value` (fixture rank 1: 4,827,967 / 24,165,041 =
  19.98%, board roi 14.93%). Label it as Lighter's own figure with an
  unknown denominator, and do not try to reproduce it.
- Lowercase `l1_address`; drop the streak, biggest-position and long/short
  fields.
- Budget: about 20 requests per refresh out of 60 per minute.

L8. **Ownership proof.** EIP-191 over the shared claim text, with the signer
equal to the L1 address. There is no vault or master indirection.

Fixtures: everything in `fixtures/lighter/`.

Exit:
- Replaying fixture account 714710 reconciles against
  `account_714710.json` within the documented funding tolerance.
- Leaderboard parse covers all 4 windows.
- Derived realized PnL matches REST `*_account_pnl` on the overlapping
  fixture rows; the sync gives identical legs with the REST accelerator on
  and off.
- Smoke passes from Fly `iad`.
- Prod `WATCH_VENUES` entry: **not** until counsel Q1 (or a written answer
  from Lighter) is on file. Founder acceptance alone is not enough, as with
  Pacifica's `UNREAD` terms. The venue doc carries `Position: PENDING
  COUNSEL` until then, which preflight (S9) enforces.

### W-P. Pacifica (`connectors/pacifica.py`). Sonnet.

**P0 gate (first task, before code).** Find and read Pacifica's Terms of
Service.
- Not found at docs.pacifica.fi or pacifica.fi on 2026-10-22. Try the app
  footer, the JS bundle, then `docs/other/contact-us`.
- Write the "Terms-of-service position" section from the actual text.
- If the terms ban API use, automated access or commercial/third-party use as
  Velocity's do, **stop and report**.
- If they cannot be found, write `Position: UNREAD`. Preflight (S9) then
  keeps Pacifica out of `WATCH_VENUES` in prod while the code still merges.

Hosts:
- REST: `https://api.pacifica.fi/api/v1`
- Websocket: `wss://ws.pacifica.fi/ws` [VERIFY URL from the websocket doc]

Never send a body or Content-Length on GETs (CloudFront answers 403).

Profile:
- `address_kind=SOLANA`, `quote_asset="USDC"`, `transfers_observable=True`
- `history_depth="all"` [VERIFY]
- `fee_rate_bps`: the base taker rate, rounded up [VERIFY tier 1: the
  fixture account at fee_level 3 pays 3.4 bps]
- `leaderboard_periods=("day","week","month","all")`
- `stream_mode=FILLS`, `stream_users_per_ip=90` (100 websocket connections
  per IP)
- `rate_budgets`: IP-scoped, 100 credits per 60 s. Heavy GETs cost 3-12;
  charge 12 for the leaderboard and history pages until verified.

P1. **Fills: `trades/history`.**
- `venue_fill_id = history_id`, cursor paging.
- `side` open_long, close_long, open_short or close_short gives the action.
- `event_type` is `fulfill_taker` / `fulfill_maker` (fixture; the scout's
  `taker`/`maker` was wrong) and maps to `liquidity` taker/maker. Any other
  value raises in a test-covered branch rather than defaulting.
- `cause` handling: only `normal` is observed in the fixtures.
  - `normal` is a fill.
  - The scout's list (`*_liquidation`, `settlement`, `game_settlement`,
    `fulfill_rfq`) is **unverified**. Treat `*_liquidation` and `settlement`
    as fills only once a fixture or the docs show them; until then any cause
    other than `normal` becomes `UnsupportedActivity` (flagged, not dropped),
    so an unknown value never silently changes the ledger.
- **PnL is net of the fee.** Fixture check: close_short 200 SOL @ 103.66,
  entry 107.490396.
  - gross = (107.490396 - 103.66) x 200 = 766.0792
  - fee 7.04888
  - gross - fee = 759.03032, against the reported pnl 759.03031
  - an open fill reports pnl = -fee

  So the REALIZED_PNL leg is `pnl + fee` (766.07919) and the FEE leg is
  `-fee`. Together they sum to the venue's pnl exactly. Make that a test.
  Confirm the 1e-5 residual is venue rounding of the gross, not a missed
  term [VERIFY].

P2. **Events.**
- `funding/history` gives FUNDING (signed `payout`, `venue_ref=history_id`).
- `account/balance/history` gives DEPOSIT and WITHDRAWAL.
  - `deposit` and `deposit_release` semantics with `pending_balance` must be
    pinned from the fixture: book the flow once, at release, or at deposit
    with the pending part excluded. Document which.
  - The fixture rows carry no id field. Use
    `venue_ref = balance:{created_at_ms}:{event_type}:{amount}:{balance}`.
    The running balance makes collisions practically impossible, and the
    fields are stable venue values, so this is a deterministic ref, not
    `ref_is_content_hash`. That flag is reserved for the daily_verified tier,
    so check this with the main session [VERIFY no id field].
  - Set `balance_after_atoms` **only** if a fixture replay shows `balance` is
    the post-event cash including trading PnL. The running `balance` (for
    example 701,155.14 after a `deposit_release`) is cash **between** trades,
    and fills, funding and balance events arrive as separate page streams.
    So the check must run over the **merged, time-ordered ledger** (all three
    streams interleaved by timestamp, then sequence). If it does not hold on
    that merged replay, or ordering ties at the same millisecond cannot be
    resolved, leave it None.
- Subaccounts: [VERIFY] whether they are separate addresses (then
  independent watches) or appear as transfers.

P3. **Balances.**
- USDC = `balance` (excludes uPnL) [VERIFY against `account_equity - sum uPnL`].
- Perp entries from `positions` (signed by side, `entry_price`).
- `equity_usd_micros = account_equity`.
- Spot balances are `UnsupportedActivity` if non-empty.

P4. **Privacy** (on `PacificaMarketData`, S2).
- Leaderboard rows with `privacy_mode: true` are dropped in
  `fetch_leaderboards`, and their normalized addresses are kept for
  `private_addresses()` (S5 sweeps from that set once per discover refresh).
- `owner_private(address)`: `account.json` carries no privacy flag (fixture),
  so it downloads the leaderboard once and returns True, False, or None when
  the address is not on it. Used only at watch time (S3).
- The venue doc's Watchlist section states the limit: an address absent from
  the leaderboard cannot be checked for privacy; the signed opt-out covers it.

P5. **Ownership proof.** `claim_text` is the shared claim format with
"Venue: Pacifica". `verify_claim` uses `addresses.verify_ed25519(address, ...)`.
There is no sub-account indirection [VERIFY Pacifica subaccount signing].

P6. **Market data.**
- `/info`: tick_size and lot_size become the instrument step; perp symbol
  becomes `<SYM>-PERP` base.
- `/info/prices`: mark, funding.
- `/book?symbol=`: bid and ask (the first level of `l[0]` and `l[1]`).
- Funding history and daily candles: endpoint names [VERIFY in REST docs].

P7. **Stream.** `account_trades` frames (`h` is history_id) are normalized by
the same `to_fill` code as REST, so it uses FILLS.

P8. **Leaderboard.** Drop `username`. Map `pnl_1d/7d/30d/all_time`,
`volume_*` and `equity_current` to `account_value`. There is no roi, so
`roi=None`.

Fixtures: `fixtures/pacifica/` (anonymise `trades_history_privacy_mode_account.json`).

Exit:
- The fixture account's trades, funding and balance history replay to
  `account.json` balance within the documented tolerance.
- Privacy rows never reach a snapshot (test).
- Opt-out works with an ed25519 signature (test).
- Smoke passes from Fly `iad`.

### W-G. GMX v2 Arbitrum (`connectors/gmx.py`). Opus, gated.

**G0. Design note** (`docs/venues/gmx_arbitrum.md` § "Account model"; accepted
by the main session and the founder before G1). Opus, no connector code. If it
is not accepted by the time Lighter and Pacifica merge, GMX moves to wave 2
(§1). The research proposes a synthetic margin account. This plan recommends
one specific form, the **retained-payout synthetic account**, for these
reasons:

- `valuation.daily_returns` puts flows at end of day:
  `r_d = (E_d - F_d)/E_(d-1) - 1`, undefined when `E_(d-1) <= 0`.
- If every increase were a deposit and every decrease a withdrawal, a trader
  who is flat at each UTC midnight would have **no defined return on any
  day**, and intraday PnL would vanish from the TWR.
- Instead, a decrease's payout stays as synthetic USDC cash in the account.
  An increase draws its collateral from that cash first, and only the
  shortfall is a `DEPOSIT` (external flow). Nothing is ever withdrawn.
  Claimed funding stays as cash too.
- Equity is synthetic cash plus the sum of position net value. It is non-zero
  while flat, so the TWR chains across flat periods.

**Known limit of the retained-payout model (must be stated in the
methodology note).** Because nothing is ever withdrawn, synthetic cash only
grows. A trader who banked $1M and now trades $10k positions has returns,
drawdown and exposure measured against $1M+, so recent returns are diluted
toward zero. That is **not** GMX's `pnl / maxCapital`, and the note must not
claim it is. G0 compares it, with the same worked example, against:
- **Alternative B, payout-as-withdrawal with a capital floor:** synthetic
  cash is kept at a floor equal to the most collateral the account has ever
  had posted at once. A decrease's payout tops cash up to that floor, and
  anything above the floor is a `WITHDRAWAL`. An increase draws from cash
  first, the shortfall is a `DEPOSIT`. The account never drops to zero while
  flat, and returns are measured against capital the trader actually put at
  risk rather than everything ever banked.
G0 picks one and states why; the other goes in the note as the rejected
option. The choice is a founder decision (§6).

**The connector is stateful.** The shortfall-vs-cash split depends on every
prior fill, so the connector carries running synthetic cash (and, per
instrument, signed size in atoms; item 4) in the opaque cursor, and always
starts from genesis when the cursor is None. A test restarts a backfill from
None mid-history and asserts identical legs (the ledger dedupes on
`venue_fill_id`, so any divergence would be a silent corruption).

The note must settle, with hand-computed worked examples:
1. Collateral scope for v1: **USD stablecoin collateral only** (USDC, USDC.e;
   6 dp) for **positions**. Trade actions with WETH or other non-stable
   collateral become `UnsupportedActivity` (flagged). In the fixtures,
   trade-action collateral for top traders is 100% USDC, so this costs little.
2. **Funding claimed in non-stable tokens is in scope.** Received funding on a
   USDC-collateral short is credited in the long token: for the top all-time
   address, `arb_squid_claims_multichain.json` token entries are USDC 120,
   WETH 42, BTC 28, SOL 10, LINK 9, AVAX 8. Flagging these would flag nearly
   every active account. So:
   - Each claim is valued in USD at the `tokenPrices` carried on that claim
     action (token amount at token decimals x price at 30 - decimals),
     rounded to 6 dp half-even (item 4), and booked as a **FUNDING leg in
     USDC-equivalent** at claim time. The token, its amount and the price go
     in the leg description.
   - The claimed token is treated as converted to USDC at that price; Koine
     does not hold it as an asset afterwards (the wallet's later use of it is
     wallet activity, out of scope).
   - Unclaimed funding is not in equity until claimed (a documented lag).
     G0 checks whether a "claimable" event gives an earlier booking point
     and, if so, uses it with the same pricing rule.
   - A second-token PnL payout on a decrease needs no special case: the
     REALIZED_PNL leg comes from `basePnlUsd`, which is already USD.
     [VERIFY on fixtures that no USDC-collateral decrease pays in a token
     whose USD value differs from `basePnlUsd` beyond the rounding.]
3. Leg mapping per orderType:
   - increase (2/3/8): FILL_BASE, FEE (position fee net of trader discount,
     plus the UI fee)
   - settled funding paid: FUNDING (negative)
   - borrowing: INTEREST (negative)
   - shortfall: DEPOSIT
   - decrease (4/5/6): FILL_BASE, REALIZED_PNL (`basePnlUsd`; decide what
     price impact counts as), FEE, FUNDING, INTEREST
   - liquidation (7): the same, plus a LIQUIDATION fee leg
   - funding claims: FUNDING (positive), item 2
   - swaps (0/1): ignored, since they are wallet activity rather than account
     activity
4. **Rounding and scaling (decided here, not later).**
   - `executionPrice` is at 10^(30 - indexTokenDecimals); USD sizes are at
     10^30. Every conversion goes through `money.fixed_to_atoms` /
     `fixed_to_decimal_str`.
   - **USD → USDC legs:** 30-dp USD values are almost never exact at 6 dp
     (fixture `basePnlUsd = 51033091798973216164946606156791229`, and
     `priceImpactUsd` likewise), and the on-chain USDC payout
     (`outputAmount`) is not public. So the GMX connector, and only it,
     calls `fixed_to_atoms(raw, 30, 6, rounding=ROUND_HALF_EVEN)`, **per
     leg**. The per-leg error is at most 0.5e-6 USD; the accumulated error on
     an account is at most 0.5e-6 x the number of rounded legs, stated in
     methodology. Synthetic cash is not reconciled (item 6), so this bound
     is the published tolerance.
   - **Quantities:** `sizeDeltaInTokens` on 18-dp index tokens cannot be
     exact at 12 dp (fixture `690631526897078958132301` loses `132301`).
     `qty_atoms` uses decimals = min(token decimals, 12) and **truncates
     toward zero** (`ROUND_DOWN`) per fill. To stop truncation dust from
     leaving a phantom position: when a decrease closes the venue position
     (size after the action is zero), the FILL_BASE quantity is the negation
     of the running ledger size from the cursor, not the truncated delta.
     The reconcile tolerance on size is 1 atom per fill since the position
     was last flat (`position_tolerance_atoms`, S2), documented.
   - Hand-computed tests for both rules, using the two fixture values above.
5. Instrument identity:
   - GMX allows simultaneous long and short on one market, and several
     collateral tokens per side. Koine nets one position per instrument.
   - Instrument symbol `<marketAddress>:L` / `<marketAddress>:S`. `base_asset`
     puts the side marker **before** the suffix: `<INDEX>-L-PERP` /
     `<INDEX>-S-PERP` (for example `ETH-L-PERP`). `valuation.PERP_SUFFIX =
     "-PERP"` is checked with `endswith` (`valuation.py:165` raises
     otherwise; also `paper.py`, `paper_signals.py:157`, `prices.py`,
     `analytics_v2.py`, `paper_lifecycle.py:292`), so `ETH-PERP-L` would
     not be a perp anywhere. Signed position is always ≥ 0 for L and ≤ 0
     for S.
   - Stripping `-PERP` leaves `ETH-L` / `ETH-S`. `GmxMarketData.pricing_symbol`,
     `fetch_quote` and `fetch_daily_closes` map both to the index token's
     price, so quotes, daily closes and paper sizing find a price under the
     stripped base.
   - Check that `valuation` perp entry replay and paper sizing handle two
     instruments sharing one index price.
6. Reconcile: `balance_observable=False`, `reconcile_mode=POSITIONS` (S2).
   Signed size per instrument against GraphQL `positions` / REST
   `/v1/positions` within `position_tolerance_atoms`. Collateral per position
   is checked against the connector's cursor state in the replay test and
   the smoke script, not in nightly reconcile. The synthetic cash cannot be
   reconciled with the venue; the account's tier is `positions_verified`
   and the methodology page says what that covers.
7. Shared changes. Known so far, owned by the scaffold or the main session,
   **never the venue agent**:
   - `money.fixed_to_atoms(..., rounding=)` (S7)
   - `reconcile_mode=POSITIONS`, `position_tolerance_atoms`, tier
     `positions_verified` (S2)
   - an audit of every `PERP_SUFFIX` site against `ETH-L-PERP` style bases
     (item 5); if any site keys prices by a base other than through market
     data (`prices_daily` rows for `ETH-L` / `ETH-S`), the main session
     fixes it. The `-L`/`-S` marker must never collide with a real index
     symbol (assert in the connector).
   G0 lists anything further (for example in `valuation.py` or `ledger.py`)
   and the main session makes it.

**G1. Connector**, after G0 is accepted. Profile:
- `display_name="GMX (Arbitrum)"`, `quote_asset="USDC"`
- `balance_observable=False`, `reconcile_mode=POSITIONS`,
  `position_tolerance_atoms` from G0 item 4, `transfers_observable=False`
- `history_depth="all"`
- `fee_rate_bps=6` (the 6 bps imbalancing position fee)
- `paper_fallback_penalty_bps`: the size-impact allowance from G0
- `leaderboard_periods=("day","week","month","all")`
- `stream_mode=None` (no public stream; polling only)
- `rate_budgets`, all IP-scoped and conservative [VERIFY]: REST 120 per
  minute and squid 120 per minute, each with cost 0 for the other host

Sources:
- Fills: GraphQL `tradeActions(where:{account_eq: evm_checksum(addr), eventName_eq:"OrderExecuted"})`
  with keyset paging on `(timestamp, id)`, `limit` 1000 or less.
  `venue_fill_id = txHash:logIndex`.
- **A lowercase address returns an empty history**, so a test asserts that
  the query uses the checksum.
- REST `/v1/trades` is a fallback only, paged by `until`. **Never by cursor
  past 10,000.**
- Claims: `claimActions`.
- GMX Account flows (`multichainSamechainFundingEvents`) become
  DEPOSIT/WITHDRAWAL if G0 keeps them in scope; otherwise
  `UnsupportedActivity`.

Market data:
- Instruments from `/markets` + `/tokens` (`tick=1`, `step=1` atom of the
  index token; `copyable=False` for SPY, QQQ, SPCX, gold, oil and other
  TradFi markets).
- Quote: Oracle `/prices/tickers` per index token, with bid = minPrice and
  ask = maxPrice, scaled.
- `pricing_symbol` / quotes / daily closes map `<INDEX>-L` and `<INDEX>-S`
  to the index token (G0 item 5).
- Funding: hourly `fundingRateSnapshots` + `borrowingRateSnapshots` folded
  into `FundingRate(rate = long funding + long borrow, short_rate = short
  funding + short borrow)` per hour (S2/S7).
- Daily closes: `/v1/prices/ohlcv?timeframe=1d`.

Leaderboard: `accountPnlRankMetrics(where:{period_eq}, orderBy: pnlUsd_DESC)`
plus a second query by `pnlBps_DESC`.
- `pnl` = `fixed_to_decimal_str(pnlUsd, 30)` passed through `leaderboard_figure`.
- `roi = pnlBps/10000`, `volume` scaled, `account_value=None`.
- Periods: today→DAY, week→WEEK, month→MONTH, all→ALL. `year` is unused.

A schema smoke check in the smoke script runs introspection and asserts that
the pinned fields exist, because the squid schema has broken before.

Ownership proof: EIP-191, signer equal to the address. Smart wallets
(EIP-1271) are a known gap, the same as on Hyperliquid.

Fixtures: `fixtures/gmx/` (the Arbitrum ones; leave out Avalanche).

Exit:
- G0 accepted.
- Fixture address replay matches `/v1/positions` size within
  `position_tolerance_atoms` and collateral exactly (cursor state).
- A backfill restarted from a None cursor reproduces identical legs.
- Non-stable funding claims book USDC-equivalent FUNDING legs at the claim's
  `tokenPrices` (hand-computed test on a WETH claim).
- Rounding tests for the 30-dp USD and 18-dp quantity fixture values.
- Leaderboard parse covers all 4 periods.
- Checksum test passes.
- Smoke (including the schema check) passes from Fly `iad`.

### W-N. Nado (`connectors/nado.py`), wave 2. Sonnet.

Same rules as the other venues. Watch only (`leaderboard_periods=()`,
`fetch_leaderboards` returns `{}`).
- Address: the EVM owner address (EVM kind). The connector fans out to
  subaccounts (bytes32 = address plus name; `subaccounts` query), as in L1.
- Data:
  - archive `matches`
  - `events` (deposit_collateral, withdraw_collateral, transfer_quote,
    liquidations)
  - `interest_and_funding`
  - gateway `subaccount_info` for reconcile
  - symbols and market_price for market data
- Every x18 value goes through `money.fixed_to_atoms(raw, 18, d)`.
- The gateway needs an `Accept-Encoding` header.
- Terms: the US is view-only. Quote the restricted-territories page and read
  the full Terms of Use for any scraping or automated-access clause [VERIFY].

---

## 4. Integration (main session, after each branch is reviewed)

1. Merge order: scaffold, then Lighter and Pacifica as each turns green, then
   GMX if G0 was accepted in time (otherwise GMX joins Nado in wave 2), then
   Nado. Resolve the marked registry, demo and seed blocks. Run
   `uv run pytest` and ruff after each merge, then `graphify update .`.
2. `watchlist.WATCHABLE_VENUES = ("hyperliquid", "polymarket", "lighter", "pacifica", "gmx_arbitrum", "nado")`.
   This order sets the discover tabs and the `/watch` venue select. The
   `config.watch_venues` default stays `"hyperliquid"`.
3. `fly.toml [env] WATCH_VENUES`: add each venue only after
   - its smoke passes from Fly `iad`, and
   - the founder accepts its terms position, and
   - for Lighter, counsel Q1 or a written Lighter answer is on file; for
     Pacifica, its terms are read. Preflight enforces both through the
     `PENDING COUNSEL` / `UNREAD` markers (S9).

   `WATCH_VENUES = "hyperliquid,polymarket"` is already on `main` (`73fbf3b`,
   PR #40); new venues are appended to it.
4. Streams:
   - `stream_users_per_ip` values land with the profiles.
   - Check that `stream.StreamSupervisor` slot leases per `EGRESS_IP` work
     for three streaming venues at once (`tests/test_stream_pg.py`, extended
     with two venues).
   - The Lighter `readonly=true` URL must be the only one the code can build.
5. Budgets: run the discover refresh, the hourly `prices` job and watch syncs
   together. Check that Lighter's 60 per minute and Pacifica's 100 credits
   per minute leave headroom at `watch_max_tracked` (500). If not, add a
   per-venue tracked cap setting, which is a shared change made here.
6. Shared methodology (`docs/methodology.md`, merged from the venue docs'
   notes):
   - Lighter derived funding and derived realized PnL (average cost)
   - Pacifica's PnL split
   - GMX's synthetic account (the model G0 chose, with its stated limit), the
     30-dp rounding rule and its error bound, non-stable funding claims
     valued at the claim's prices, and the `positions_verified` tier
   - the venue privacy sweep and its limit (addresses absent from a board)
   - `not_copyable`
   - asymmetric funding
   - external flows on venues without a balance
7. `docs/security.md`: no new secrets or hosts in the CSP (all calls are
   server side). Solana signature verification is listed under ownership
   proofs.
8. Status and project docs:
   - `docs/copy_strategies_implementation.md` status gains a "2026-10 on-chain
     perps" entry.
   - `BUILD_PLAN.md` scope: on-chain perp DEXs for watching public addresses,
     reversing the earlier "no on-chain" line.
   - `docs/implementation_plan.md` §8 status.
   - The CLAUDE.md layout lists the new connectors, `addresses.py` and
     migration 0019.
   - `docs/worker.md` covers the new IP budgets.
   - Memory note: platform expansion.
9. Demo: `scripts/seed_demo.py` into a `*_demo` database. Click through
   `/watch/discover` tabs for each new venue and one watched address per
   venue.

---

## 5. Exit criteria

**Per venue:**
- The profile, connector and market data are registered. `build_connector`
  and `build_market_data` work.
- respx tests cover fills, events, balances, leaderboard, quote, funding
  history, the claim/opt-out signature, and stream frame parsing where the
  venue has one.
- Every formula has a hand-computed test.
- A full fixture replay reconciles (exact, or within a tolerance documented
  in the venue doc and the methodology).
- `docs/venues/<venue>.md` is complete, including the Watchlist, Leaderboard
  and Methodology notes sections.
- `scripts/<venue>_smoke.py` passes from this machine **and from a Fly `iad`
  machine**.
- Demo rows render.
- `uv run pytest` and ruff are green.

**Overall:**
- Hyperliquid and Polymarket behave unchanged.
- A Solana address round-trips through watch, opt-out and discover with its
  case intact (the scaffold test: `snapshot_row(venue, entry)` →
  `LeaderboardSnapshot.rows` → `/watch/discover` → `add_watch` →
  `watch_account`, plus Pacifica's real fixture address end to end).
- A `positions_verified` account reconciles positions nightly and never
  reads cash.
- No surface shows a name, username, ENS or handle.
- Preflight refuses an unread terms doc in `WATCH_VENUES`.
- Paper copying skips `not_copyable` instruments.
- The founder signs off on each venue's terms position before its prod
  `WATCH_VENUES` entry; Lighter additionally needs counsel Q1 answered.

### Open [VERIFY] items

**Lighter:**
- Multi-trade tx hashes.
- Which explorer `status` values carry real trades (`nothing_to_execute`
  appears on a REST-listed trade).
- Replayed average entry vs `taker_entry_quote_before` on every overlapping
  row.
- Fee rounding mode.
- Funding settlement model (prefix sums) and sign.
- roi unit (denominator unknown; not reproduced, labelled as Lighter's).
- Whether the leaderboard aggregates sub-accounts and pool shares.
- Market category field.
- `orderBookOrders` weight.
- Whether unauthenticated `trades` and `pnl` stay open (the accelerator
  degrades if not).
- Behaviour from a Fly datacenter IP.

**Pacifica:**
- Where the terms live and what they say.
- Base fee tier.
- `cause` values other than `normal`.
- `deposit` vs `deposit_release` semantics.
- Whether balance-history ids exist.
- Whether `balance` is the full running cash.
- Subaccount model.
- Whether any per-account endpoint carries the privacy flag
  (`account.json` does not).
- Websocket URL.
- Funding and candle endpoints.
- Heavy-GET credit costs.
- History depth.

**GMX:**
- How common wallet mode is versus GMX Account.
- Second-token payouts for USDC collateral (USD leg from `basePnlUsd`
  either way).
- Whether a "claimable funding" event gives an earlier booking point than
  the claim.
- Whether `-L`/`-S` ever collides with a real index symbol.
- Squid and API rate limits.
- PositionChange id collisions.
- SQD Cloud terms.
- The EventEmitter topic layout (only for a future stream).

**Nado:** full Terms of Use; whether a UI leaderboard exists.

### Counsel questions

1. Lighter's terms name the **API** and exclude US-incorporated entities as
   "Prohibited Persons" (one step broader than Hyperliquid's interface-only
   terms), and list "unauthorized access to the API" as a suspension ground
   while the docs mark `trades` and `pnl` as auth-only. Does read-only
   public-data use (explorer logs, and the REST endpoints) by a US company
   fall under that? Should we ask Lighter for a Builder/data arrangement?
   **This answer, or a written answer from Lighter, gates Lighter's prod
   `WATCH_VENUES` entry.**
2. GMX's and Pacifica's interface terms exclude US persons from trading. Is
   paper-copying and profiling third-party addresses on such venues, for US
   users, any different from Hyperliquid (already shipped)?
3. Synthetic US equities and ETFs (Lighter's AAPL and SPY; GMX's SPY and
   QQQ), pre-IPO perps (Lighter's ANTHROPIC, OPENAI and SPACEX), FX and
   commodities. They are watchable but `copyable=False` until answered. Can
   they be paper-copied for US users?
4. Privacy: profiling third-party addresses across several venues. Each venue
   is a separate watch and is never linked (no cross-venue matching). Is the
   opt-out (signature, or the venue's own privacy flag) enough?
5. dYdX: confirm the no-go reading. Read-only indexer use by a US company
   counts as using the perps software, and the API geoblock rules out any
   workaround.

---

## 6. Decisions the founder makes before the build starts

**Resolved 2026-10-22:** (1) yes, watch-only: on-chain venues are watchable
by public address, never wallet connects. (2) Demo first: no new venue enters
prod `WATCH_VENUES` until its terms position is cleared; preflight enforces
the markers. Lighter's data arrangement approach is the founder's to send.
(3) Lighter REST accelerator **off** (explorer only) until Q1 clears.
(6) and (7) accepted as written. (4) and (5) wait for the G0 note. (8)
counsel questions are held with the founder's other counsel items.

1. **Reverse the "no on-chain" scope line** in `BUILD_PLAN.md` for watching
   public addresses on Lighter, Pacifica and GMX (wave 1) and Nado (wave 2).
2. **Lighter's prod gate:** accept that Lighter can merge and run on the demo
   but stays out of prod `WATCH_VENUES` until counsel Q1 (or a written Lighter
   answer) is on file. Optionally, approach Lighter for a data arrangement now
   so the answer arrives in parallel.
3. **Lighter's REST accelerator default:** off until Q1 clears (explorer only,
   slower live edge), or on with automatic fallback.
4. **GMX account model:** retained-payout (returns diluted by banked profit)
   vs payout-as-withdrawal with a capital floor (G0 recommends one with worked
   examples; the founder picks).
5. **GMX numeric policy:** accept a published rounding rule for GMX (USD legs
   half-even at 6 dp, quantities truncated at 12 dp with a full-close
   correction) and the new `positions_verified` tier, whose public wording
   says cash is reconstructed, not checked. Rejecting either means GMX is not
   viable in v1.
6. **GMX slip:** agree that GMX moves to wave 2 if G0 is not accepted by the
   time Lighter and Pacifica merge.
7. **Pacifica:** if its terms cannot be found, accept that it merges as
   `UNREAD` and stays out of prod; if they ban API or commercial use, drop it.
8. **Counsel:** send questions 1-4 (Q1 is blocking for Lighter in prod; Q3
   decides whether synthetic equities, FX and commodities stay
   `copyable=False`).

---

## Review log (2026-10-22)

All MUST-FIX items (1-9) applied. SHOULD-FIX:

- 10 applied: the limit is stated and alternative B is in G0's comparison;
  the choice is founder decision 4.
- 11 applied: stateful cursor, genesis start, restart test (G0 and G1 exit);
  the same rule is applied to Lighter's derived PnL replay.
- 12 applied: Lighter is `NUDGE`.
- 13 applied.
- 14 applied: required fields pinned in the common profile rules.
- 15 applied: kept as a regression pin only, no code change.
- 16 applied: `private_addresses` and `owner_private(address)` live on
  `MarketDataSource`.
- 17 applied: `fulfill_taker`/`fulfill_maker` mapping; non-`normal` causes
  flagged until verified.
- 18 applied.
- 19 applied: `eth-utils` added as a direct dependency.
- 20 partly rejected: no `"synthetic"` asset class now, because watch
  strategies are never public, so the marketplace filter never sees these
  venues; the note is in §1, to revisit if a venue opens for connects.
- 21 applied (scaffold exit and overall exit).
- 22 applied: GMX starts as G0 only and slips to wave 2 if not accepted in
  time.

Found while applying item 8 (beyond the review's list): `watchlist.py:166`
and `jobs.py:388` also compare through `func.lower(VenueAccount.key_id)` and
would miss mixed-case Solana addresses; both are in S4's table, with a
pre-merge check that stored EVM addresses are already lowercase.
