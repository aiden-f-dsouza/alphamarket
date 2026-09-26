"""Record ONE public, unauthenticated venue fixture (P0 hygiene, invariant #6).

Invariant #6 says tests never make live API calls: fixtures are recorded once,
outside the test run, and replayed with respx. This script is the only
sanctioned way real data reaches `tests/fixtures/`: it makes exactly one GET
or POST-JSON request to a PUBLIC, unauthenticated venue endpoint, writes the
pretty-printed JSON response to
`tests/fixtures/<venue>/<name>_<YYYYMMDD>.json`, and appends a provenance
line (url, params, date) to `tests/fixtures/<venue>/README.md`.

It refuses to run if the URL, any param, or any header looks like it would
carry a credential (matches "key", "secret", "token", "signature" or
"authorization", case-insensitively) -- this script must never be the way a
real API key reaches disk or a fixture file.

Usage:
  uv run python scripts/record_fixture.py kalshi market_btc \\
      https://demo-api.kalshi.co/trade-api/v2/markets/KXBTC-25 --get

  uv run python scripts/record_fixture.py polymarket activity \\
      https://data-api.polymarket.com/activity --get \\
      --param user=0xabc... --param type=TRADE

  uv run python scripts/record_fixture.py somevenue quote \\
      https://api.example.com/quote --post-json '{"symbol": "BTC-USD"}'
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

BANNED_SUBSTRINGS = ("key", "secret", "token", "signature", "authorization")

FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


class CredentialLikeInput(ValueError):
    """The URL, a param, or a header looks like it would carry a credential."""


def _check_not_credential_like(label: str, value: str) -> None:
    lowered = value.lower()
    for banned in BANNED_SUBSTRINGS:
        if banned in lowered:
            raise CredentialLikeInput(
                f"{label} {value!r} looks like a credential (matches {banned!r}); "
                "this script only records PUBLIC, unauthenticated requests"
            )


def _check_request(url: str, params: dict[str, str], headers: dict[str, str]) -> None:
    _check_not_credential_like("url", url)
    for k, v in params.items():
        _check_not_credential_like("param name", k)
        _check_not_credential_like(f"param value ({k})", v)
    for k, v in headers.items():
        _check_not_credential_like("header name", k)
        _check_not_credential_like(f"header value ({k})", v)


def record(
    venue: str,
    name: str,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, str] | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    fixtures_root: Path | None = None,
) -> Path:
    """Make ONE public request and write its JSON response to a fixture file.

    Returns the path written. Raises CredentialLikeInput before any request
    is sent if the url/params/headers look unsafe, and httpx.HTTPError if the
    request itself fails.
    """
    params = params or {}
    headers = headers or {}
    method = method.upper()
    _check_request(url, params, headers)

    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        if method == "GET":
            resp = client.get(url, params=params, headers=headers)
        elif method == "POST":
            resp = client.post(url, params=params, json=json_body or {}, headers=headers)
        else:
            raise ValueError(f"unsupported method {method!r}; use GET or POST")
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if owns_client:
            client.close()

    now = now or datetime.now(UTC)
    root = fixtures_root or FIXTURES_ROOT
    venue_dir = root / venue
    venue_dir.mkdir(parents=True, exist_ok=True)

    fixture_path = venue_dir / f"{name}_{now.strftime('%Y%m%d')}.json"
    fixture_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    provenance = (
        f"- `{fixture_path.name}` -- recorded {now.date().isoformat()} "
        f"from public endpoint `{method} {url}`"
    )
    if params:
        provenance += f" params={params}"
    provenance += " by scripts/record_fixture.py; re-record from a real account when one exists.\n"
    readme_path = venue_dir / "README.md"
    is_new = not readme_path.exists() or readme_path.stat().st_size == 0
    with readme_path.open("a") as f:
        if is_new:
            f.write(f"# {venue} fixtures\n\n")
        f.write(provenance)

    return fixture_path


def _split_pairs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        k, _, v = pair.partition("=")
        out[k] = v
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("venue")
    parser.add_argument("name")
    parser.add_argument("url")
    parser.add_argument("--get", action="store_true", help="GET request (the default)")
    parser.add_argument("--post-json", metavar="JSON", help="POST this JSON body")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--header", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)

    params = _split_pairs(args.param)
    headers = _split_pairs(args.header)
    method = "POST" if args.post_json is not None else "GET"
    json_body = json.loads(args.post_json) if args.post_json is not None else None

    try:
        path = record(
            args.venue,
            args.name,
            args.url,
            method=method,
            params=params,
            json_body=json_body,
            headers=headers,
        )
    except CredentialLikeInput as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
