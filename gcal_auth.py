"""Google Calendar sign-in for the tablet dashboard: OAuth with PKCE (used by the admin page) and the
command line.

Command line (run on the server, in a terminal):
  python3 gcal.py auth      one-time sign-in (prompts for the client ID/secret if missing)
  python3 gcal.py status    show what is configured
  python3 gcal.py test      fetch once and print a summary
  python3 gcal.py revoke    revoke the refresh token at Google and delete it locally
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

import envfile
from gcal import (AUTH_URL, CLIENT_ID_ENV, CLIENT_ID_RE, CLIENT_SECRET_ENV, SCOPE, TOKEN_RE, TOKEN_URL, CalendarError,
                  CalendarService, _http, load_client, load_refresh_token, revoke_token, set_env_values, write_private)
from upstream import NoRedirect


def pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id, redirect_uri, state, challenge, login_hint=""):
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def parse_redirect(text, expected_state):
    """Return the code from a redirect URL or query string. Raises ValueError with a message."""
    text = (text or "").strip()
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("/?")
    if "?" in query:
        query = query.split("?", 1)[1]
    q = urllib.parse.parse_qs(query)
    if q.get("error"):
        raise ValueError("Google returned an error: %s" % q["error"][0][:60])
    if q.get("state", [""])[0] != expected_state:
        raise ValueError("The state value does not match this sign-in attempt. Start again.")
    code = q.get("code", [""])[0]
    if not code or len(code) > 1024:
        raise ValueError("No authorization code found in that URL.")
    granted = q.get("scope", [SCOPE])[0].split()
    if SCOPE not in granted:
        raise ValueError("Calendar access was not granted. Start again and tick the "
                         "\"See events on all your calendars\" box.")
    return code


def exchange_code(client, code, verifier, redirect_uri, timeout=15):
    form = urllib.parse.urlencode({
        "client_id": client[0], "client_secret": client[1], "code": code,
        "code_verifier": verifier, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=form, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        data = _http(opener, req, timeout, 65536)
    except CalendarError as exc:
        if exc.code == "http":
            body = exc.detail[1]
            raise ValueError("Google rejected the sign-in (%s: %s)" % (
                str(body.get("error", exc.detail[0]))[:40], str(body.get("error_description", ""))[:120]))
        raise ValueError("Could not reach Google (%s)" % exc.code)
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not TOKEN_RE.match(refresh):
        raise ValueError("Google did not return a refresh token. Remove the app's access at "
                         "https://myaccount.google.com/connections and try again.")
    if SCOPE not in str(data.get("scope", "")).split():
        raise ValueError("Calendar access was not granted. Start again and tick the calendar box.")
    return refresh


def _cli_paths():
    import config  # local import: only for the command line
    cfg = config.load_config(os.path.join(config.BASE_DIR, "config.json"))
    c = cfg["calendar"]
    return (cfg, os.path.join(config.BASE_DIR, c["env_file"]),
            os.path.join(config.BASE_DIR, c["token_file"]))


def _prompt_client(env_path):
    import getpass
    print("Paste the OAuth client details from Google Cloud Console")
    print("(Google Auth Platform > Clients > your Desktop client).")
    cid = input("Client ID: ").strip()
    if not CLIENT_ID_RE.match(cid):
        sys.exit("That doesn't look like a client ID (…apps.googleusercontent.com). Nothing saved.")
    secret = getpass.getpass("Client secret (input hidden): ").strip()
    if not envfile.KEY_RE.match(secret):
        sys.exit("That doesn't look like a client secret. Nothing saved.")
    set_env_values(env_path, {CLIENT_ID_ENV: cid, CLIENT_SECRET_ENV: secret})
    print("Saved to %s (mode 600)." % env_path)


def cmd_auth(args):
    import select
    from http.server import BaseHTTPRequestHandler, HTTPServer
    _, env_path, token_path = _cli_paths()
    client, problem = load_client(env_path)
    if problem or "--new-client" in args:
        if problem == "insecure_file":
            sys.exit("%s must be a regular file owned by you with mode 600." % env_path)
        _prompt_client(env_path)
        client, problem = load_client(env_path)
        if problem:
            sys.exit("Client details could not be read back (%s)." % problem)

    result = {}
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()

    class Catch(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                result["code"] = parse_redirect(self.path, state)
                msg = "Signed in. You can close this tab and return to the terminal."
            except ValueError as exc:
                result.setdefault("error", str(exc))
                msg = str(exc)
            body = ("<!doctype html><meta charset=utf-8><title>Dashboard calendar</title>"
                    "<p style='font:18px sans-serif;margin:2em'>%s</p>" % msg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = 0
    for a in args:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    srv = HTTPServer(("127.0.0.1", port), Catch)
    redirect_uri = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hint = next((a.split("=", 1)[1] for a in args if a.startswith("--login-hint=")), "")
    url = build_auth_url(client[0], redirect_uri, state, challenge, hint)

    print("\n1. Open this URL in a browser and sign in with the calendar's Google account:\n")
    print(url)
    print("\n2. If Google says the app isn't verified: Advanced > Go to … (unsafe). It is your own app.")
    print("3. Tick the calendar permission box and continue.")
    print("\nIf the browser runs on this server, sign-in finishes by itself. If it runs on another")
    print("computer, the last page will fail to load (\"can't reach 127.0.0.1\"): copy that page's")
    print("full address from the address bar and paste it here, then press Enter.\n")
    deadline = time.monotonic() + 600
    try:
        while "code" not in result and time.monotonic() < deadline:
            if "error" in result:
                sys.exit(result["error"])
            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            if ready:
                line = sys.stdin.readline()
                if not line:
                    time.sleep(1)
                    continue
                if line.strip():
                    try:
                        result["code"] = parse_redirect(line, state)
                    except ValueError as exc:
                        print(exc)
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    finally:
        srv.shutdown()
    if "code" not in result:
        sys.exit("Timed out after 10 minutes. Run it again.")
    try:
        refresh = exchange_code(client, result["code"], verifier, redirect_uri)
    except ValueError as exc:
        sys.exit(str(exc))
    write_private(token_path, json.dumps({
        "refresh_token": refresh, "scope": SCOPE,
        "created": datetime.now().astimezone().isoformat(timespec="seconds")}) + "\n")
    print("Signed in. Token saved to %s (mode 600)." % token_path)
    print("The running dashboard picks it up within a minute; no restart needed.")


def cmd_status(_args):
    cfg, env_path, token_path = _cli_paths()
    c = cfg["calendar"]
    _, problem = load_client(env_path)
    print("enabled:       %s" % c["enabled"])
    print("calendars:     %s" % ", ".join(c["calendar_ids"]))
    print("client:        %s" % ("ok" if not problem else problem))
    _, _, problem = load_refresh_token(token_path)
    print("sign-in token: %s" % ("ok" if not problem else problem))


def cmd_test(_args):
    import server
    from zoneinfo import ZoneInfo
    cfg, _, _ = _cli_paths()
    svc = CalendarService(cfg["calendar"], ZoneInfo(cfg["timezone"]), server.BASE_DIR, server.USER_AGENT)
    ok = svc.fetch_once()
    snap = svc.snapshot()
    if not ok:
        sys.exit("Failed: %s. %s" % (snap["problem"], snap["message"]))
    print("OK: %d events over %d days%s." % (snap["count"], cfg["calendar"]["days_ahead"],
                                             " (truncated)" if snap["truncated"] else ""))
    for d in snap["days"]:
        for ev in d["events"][:3]:
            print("  %s  %-11s %s" % (d["date"], "all day" if ev["all_day"] else ev["start_hm"] + "-" + ev["end_hm"],
                                      ev["title"]))


def cmd_revoke(_args):
    _, _, token_path = _cli_paths()
    for line in revoke_token(token_path):
        print(line)


def main(argv):
    cmds = {"auth": cmd_auth, "status": cmd_status, "test": cmd_test, "revoke": cmd_revoke}
    if not argv or argv[0] not in cmds:
        print(__doc__.split("Command line")[1].split("\n", 1)[1].rstrip())
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cmds[argv[0]](argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
