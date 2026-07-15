# -*- coding: utf-8 -*-
"""
pinterest_oauth.py
------------------
One-shot OAuth 2.0 token fetcher for Pinterest API v5.

Usage:
    python pinterest_oauth.py

What this does:
  1. Reads PINTEREST_APP_ID and PINTEREST_APP_SECRET from .env
  2. Starts a local HTTP server on port 8080 to auto-capture the redirect code
  3. Opens the Pinterest authorization page in your browser
  4. Catches the code automatically when Pinterest redirects back
  5. Exchanges the code for an access_token + refresh_token
  6. Writes PINTEREST_ACCESS_TOKEN and PINTEREST_REFRESH_TOKEN to .env
  7. Also calls GET /v5/boards and lists your boards so you can pick PINTEREST_BOARD_ID

Prerequisites (one-time setup in Pinterest Developer Portal):
  https://developers.pinterest.com/apps/<your-app-id>/
  -> "Configure" tab -> "Redirect URIs" -> Add: http://localhost:8080
  -> Save

Then add to .env:
  PINTEREST_APP_ID=1570565
  PINTEREST_APP_SECRET=<your secret from the app page>
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env", override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
APP_ID       = os.getenv("PINTEREST_APP_ID", "1570565")
APP_SECRET   = os.getenv("PINTEREST_APP_SECRET", "")
REDIRECT_URI = "http://localhost:8080"
SCOPES       = "boards:read,boards:write,pins:read,pins:write"
STATE        = "holistic_engine_auth"
ENV_PATH     = _ROOT / ".env"

AUTH_URL = (
    f"https://www.pinterest.com/oauth/"
    f"?client_id={APP_ID}"
    f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
    f"&response_type=code"
    f"&scope={urllib.parse.quote(SCOPES, safe='')}"
    f"&state={STATE}"
)

TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"

# ---------------------------------------------------------------------------
# Local callback server (catches the redirect code automatically)
# ---------------------------------------------------------------------------
_captured_code: list[str] = []
_captured_error: list[str] = []


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if "error" in params:
            _captured_error.append(params["error"])
            self._respond(
                f"<h2>Authorization Error</h2><p>{params.get('error_description', params['error'])}</p>"
            )
        elif "code" in params:
            _captured_code.append(params["code"])
            self._respond(
                "<h2>Authorization successful!</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
            )
        else:
            self._respond("<h2>Waiting...</h2>")

    def _respond(self, body: str):
        html = f"<html><body style='font-family:sans-serif;padding:40px'>{body}</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *args):  # silence default access logs
        pass


def _run_server(server: HTTPServer):
    server.handle_request()   # serve exactly one request


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

def _exchange_code(code: str) -> dict:
    if not APP_SECRET:
        print("\nERROR: PINTEREST_APP_SECRET is not set in .env.")
        print("Add it: PINTEREST_APP_SECRET=<your secret from developers.pinterest.com/apps/1570565>")
        sys.exit(1)

    credentials = base64.b64encode(f"{APP_ID}:{APP_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = urllib.parse.urlencode({
        "grant_type":   "authorization_code",
        "code":         code,
        "redirect_uri": REDIRECT_URI,
    }).encode()

    req = urllib.request.Request(TOKEN_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# .env writer
# ---------------------------------------------------------------------------

def _update_env(key: str, value: str) -> None:
    """Update or append a key=value line in .env."""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.is_file() else ""
    pattern = rf"^{re.escape(key)}\s*=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, text, re.MULTILINE):
        text = re.sub(pattern, new_line, text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{new_line}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    print(f"  .env updated: {key}={value[:20]}...")


# ---------------------------------------------------------------------------
# Board discovery
# ---------------------------------------------------------------------------

def _list_boards(token: str) -> list[dict]:
    import requests  # noqa: PLC0415
    headers = {"Authorization": f"Bearer {token}"}
    boards, url = [], "https://api.pinterest.com/v5/boards"
    params = {"page_size": 100}
    while url:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            print(f"  Boards API returned HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        boards.extend(data.get("items", []))
        cursor = data.get("bookmark")
        params = {"bookmark": cursor} if cursor else {}
        url = "https://api.pinterest.com/v5/boards" if cursor else None
    return boards


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("\n=== Pinterest OAuth 2.0 Token Generator ===\n")

    if not APP_SECRET:
        print("MISSING: PINTEREST_APP_SECRET is not set in .env.")
        print(f"\nGet it from: https://developers.pinterest.com/apps/{APP_ID}/")
        print('Add to .env:  PINTEREST_APP_SECRET=<your-secret>\n')
        print("Then re-run this script.")
        sys.exit(1)

    # Start local callback server
    server = HTTPServer(("localhost", 8080), _CallbackHandler)
    t = threading.Thread(target=_run_server, args=(server,), daemon=True)
    t.start()

    print(f"Local callback server listening on {REDIRECT_URI}")
    print(f"\nOpening Pinterest authorization page...")
    print(f"URL: {AUTH_URL}\n")
    time.sleep(1)
    webbrowser.open(AUTH_URL)

    print("Waiting for you to authorize in the browser...")
    t.join(timeout=120)

    if _captured_error:
        print(f"\nAuthorization denied: {_captured_error[0]}")
        sys.exit(1)

    if not _captured_code:
        print("\nTimeout: no code received within 120 seconds.")
        print(f"Open this URL manually, authorize, then paste the 'code=' value here.")
        print(f"\n{AUTH_URL}\n")
        code = input("Paste code here: ").strip()
        if not code:
            sys.exit(1)
    else:
        code = _captured_code[0]
        print(f"\nCode captured automatically: {code[:20]}...")

    # Exchange for token
    print("\nExchanging code for access token...")
    try:
        result = _exchange_code(code)
    except Exception as exc:  # noqa: BLE001
        print(f"Token exchange failed: {exc}")
        sys.exit(1)

    access_token   = result.get("access_token", "")
    refresh_token  = result.get("refresh_token", "")
    token_type     = result.get("token_type", "")
    expires_in     = result.get("expires_in", "?")
    refresh_in     = result.get("refresh_token_expires_in", "?")

    if not access_token:
        print(f"No access_token in response: {result}")
        sys.exit(1)

    print(f"\nToken received!")
    print(f"  Type          : {token_type}")
    print(f"  Expires in    : {expires_in}s")
    print(f"  Refresh in    : {refresh_in}s (~{int(refresh_in)//86400} days)" if str(refresh_in).isdigit() else "")

    # Write to .env
    print("\nWriting to .env...")
    _update_env("PINTEREST_ACCESS_TOKEN", access_token)
    if refresh_token:
        _update_env("PINTEREST_REFRESH_TOKEN", refresh_token)

    # Discover boards
    print("\nFetching your boards...\n")
    boards = _list_boards(access_token)

    target_id = None
    if boards:
        print(f"Found {len(boards)} boards:")
        for b in boards:
            name = b.get("name", "?")
            bid  = b.get("id", "?")
            match = "ancient wisdom" in name.lower() or "longevity" in name.lower()
            marker = "  <-- MATCH" if match else ""
            print(f"  [{bid}]  {name}{marker}")
            if match:
                target_id = bid

        if target_id:
            print(f"\nAuto-detected board ID: {target_id}")
            _update_env("PINTEREST_BOARD_ID", target_id)
            print(f"  .env updated: PINTEREST_BOARD_ID={target_id}")
        else:
            print("\nCould not auto-detect 'Ancient Wisdom & Longevity'.")
            print("Copy the ID from the list above and paste it below:")
            manual_id = input("Board ID: ").strip()
            if manual_id:
                _update_env("PINTEREST_BOARD_ID", manual_id)
    else:
        print("No boards returned (or token lacks boards:read scope).")

    print("\nDone. Run the readiness check next:")
    print("  python pinterest_main.py check-readiness\n")


if __name__ == "__main__":
    main()
