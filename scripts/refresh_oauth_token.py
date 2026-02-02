"""
Refresh Google OAuth token pickle for Google Docs.

Usage:
  python3 scripts/refresh_oauth_token.py
  python3 scripts/refresh_oauth_token.py --credentials credentials.json
  python3 scripts/refresh_oauth_token.py --scopes "scope1,scope2"
  python3 scripts/refresh_oauth_token.py --token-out token.pickle --base64-out token_base64.txt

Input:
  - OAuth client credentials JSON file path (default: news_bot.core.config.OAUTH_CREDENTIALS_FILE)
  - OAuth scopes list (comma-separated, default: news_bot.core.config.GOOGLE_DOCS_SCOPES)

Output:
  - token.pickle written to disk
  - base64 token printed to stdout (and optionally written to a file)
"""

import argparse
import base64
import json
import os
import pickle
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def load_client_config(path: str) -> dict:
    raw_value = ""
    with open(path, "r") as handle:
        raw_value = handle.read().strip()
    decoder = json.JSONDecoder()
    parsed, end = decoder.raw_decode(raw_value)
    trailing = raw_value[end:].strip()
    assert trailing == "", f"Extra data after JSON in {path}"
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    assert isinstance(parsed, dict), "Client config must be a JSON object"
    return parsed


def run_oauth_flow(client_config: dict, scopes: list[str]):
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(client_config, scopes)
    return flow.run_local_server(port=0)


def serialize_credentials(creds) -> tuple[bytes, str]:
    token_bytes = pickle.dumps(creds)
    token_base64 = base64.b64encode(token_bytes).decode("utf-8")
    return token_bytes, token_base64


def write_binary(path: str, data: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(data)


def write_text(path: str, data: str) -> None:
    with open(path, "w") as handle:
        handle.write(data)


def parse_scopes(raw_scopes: str | None, default_scopes: list[str]) -> list[str]:
    if not raw_scopes:
        return list(default_scopes)
    scopes = [s.strip() for s in raw_scopes.split(",") if s.strip()]
    assert scopes, "Scopes list cannot be empty"
    return scopes


def main(argv: list[str] | None = None) -> int:
    from news_bot.core import config

    parser = argparse.ArgumentParser(description="Refresh Google OAuth token pickle.")
    parser.add_argument("--credentials", default=config.OAUTH_CREDENTIALS_FILE)
    parser.add_argument("--scopes", default=None)
    parser.add_argument("--token-out", default=config.OAUTH_TOKEN_PICKLE_FILE)
    parser.add_argument("--base64-out", default=None)
    args = parser.parse_args(argv)

    assert os.path.exists(args.credentials), f"Credentials file not found: {args.credentials}"

    scopes = parse_scopes(args.scopes, config.GOOGLE_DOCS_SCOPES)
    client_config = load_client_config(args.credentials)
    creds = run_oauth_flow(client_config, scopes)

    token_bytes, token_base64 = serialize_credentials(creds)
    write_binary(args.token_out, token_bytes)
    if args.base64_out:
        write_text(args.base64_out, token_base64)

    print("GOOGLE_OAUTH_TOKEN_PICKLE_BASE64:")
    print(token_base64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
