# Websocket frame fixtures (P7 streaming)

Frames the stream process parses, one JSON object per named frame. No test
opens a socket (invariant 6): tests feed these through
`StreamingSource.parse` directly, or through a fake transport in
`tests/test_stream.py`.

| File | Venue / channel | Provenance |
|---|---|---|
| `hyperliquid_ws.json` | Hyperliquid `userFills` | Envelopes are docs examples (Websocket > Subscriptions), checked against a one-off public probe on 2026-10-04: `subscriptionResponse`, then a `userFills` snapshot (`isSnapshot`, `user`, `fills`; 30 fills whose keys equal the REST row keys), and `{"channel":"pong"}` for a ping. Nothing from the probe is stored. The fill objects inside are REAL rows of `../hyperliquid/userFillsByTime_2026-09-26.json`: the docs define `WsFill` as the REST fill object, which is what lets the stream ingest fills directly. |
| `kraken_ws_v2.json` | Kraken WS v2 `executions` | Docs examples (docs.kraken.com websocket-v2 executions, heartbeat, status, ping; checked 2026-10-04), reconstructed. Never seen on a real key [VERIFY]. |
| `coinbase_ws_user.json` | Coinbase Advanced Trade `user` + `heartbeats` | Docs examples (docs.cdp.coinbase.com Advanced Trade WebSocket channels; checked 2026-10-04), reconstructed. Never seen on a real key [VERIFY]. |

Re-record the Kraken and Coinbase frames once the founder accounts exist
(docs/venues/<venue>.md lists the open [VERIFY] items).
