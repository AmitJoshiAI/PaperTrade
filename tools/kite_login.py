"""Daily Zerodha Kite Connect login on the user's PC: makes the day's access token and writes it to the
GitHub secret KITE_ACCESS_TOKEN, so the 17:33 IST daily run (GitHub Actions) can read Zerodha data.
DATA ONLY: nothing here, or anywhere in this repository, places, changes or cancels an order. See
KITE_SETUP.md for the whole design and the user's one-time steps.

Run from the project folder with the venv python:

  python tools/kite_login.py --setup          store the API key and secret (and, for automatic login, the
                                              Zerodha user ID, password and TOTP secret) in Windows
                                              Credential Manager (keyring service "papertrade-kite").
                                              Prompts hide what you type; nothing is echoed or logged.
  python tools/kite_login.py                  log in, get today's access token, write it to GitHub
                                              (gh secret set KITE_ACCESS_TOKEN, token on stdin) and keep a
                                              copy in Credential Manager for local runs today. Uses
                                              automatic login if a TOTP secret is stored, else manual.
  python tools/kite_login.py --manual         open the Kite login page in the browser and catch the
                                              redirect on http://127.0.0.1:5555/callback (waits 5 minutes)
  python tools/kite_login.py --auto           log in by itself with the stored user ID, password and TOTP
  python tools/kite_login.py --if-needed      (with any mode) do nothing if today's token already works
  python tools/kite_login.py --scheduled      what the scheduled task runs: --if-needed, and also do nothing
                                              outside Monday-Friday 07:00-18:30 IST (a token is only used by
                                              the evening run) or, in manual mode, when the browser page was
                                              already opened in the last 55 minutes
  python tools/kite_login.py --install-task   register the Windows Task Scheduler task "PaperTrade Kite
                                              login", running this script with --scheduled as the current
                                              user: at sign-in, at unlock, when a network connects, and every
                                              hour 09:00-18:00 on weekdays (local time = IST on this PC)
  python tools/kite_login.py --check          print whether a token for today exists and whether
                                              profile() works with it; changes nothing

Exit codes:
  0  done (token written, or --if-needed and today's token already works, or --check: token works)
  1  setup missing: no credentials stored, a package missing, or bad arguments
  2  login failed: no request token (wrong password / TOTP, page changed, manual login timed out)
  3  token exchange failed (generate_session refused the request token: wrong API secret, token reused)
  4  token made and stored locally, but writing the GitHub secret failed (gh missing or not logged in)
  5  --check: no token for today, or the token does not work
  6  --install-task failed

Log: tools/kite_login.log (git-ignored). It records steps and errors only, never a key, secret, password,
TOTP code, request token or access token.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = Path(__file__).resolve().parent / "kite_login.log"
SERVICE = "papertrade-kite"          # same as papertrade/kite.py
REPO = "AmitJoshiAI/PaperTrade"
SECRET_NAME = "KITE_ACCESS_TOKEN"
TASK_NAME = "PaperTrade Kite login"
CALLBACK_HOST, CALLBACK_PORT, CALLBACK_PATH = "127.0.0.1", 5555, "/callback"
MANUAL_TIMEOUT = 300                 # seconds to wait for the browser redirect
KITE_WEB = "https://kite.zerodha.com"
IST = timezone(timedelta(hours=5, minutes=30))

OK, E_SETUP, E_LOGIN, E_EXCHANGE, E_GH, E_CHECK, E_TASK = 0, 1, 2, 3, 4, 5, 6

log = logging.getLogger("kite_login")


class Fail(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def _setup_logging() -> None:
    logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")


def say(msg: str) -> None:
    print(msg)
    log.info(msg)


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


# ---------- Credential Manager ----------
def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        raise Fail(E_SETUP, f"the keyring package is not installed for {sys.executable}: run this script with .venv\\Scripts\\python.exe")


def kget(name: str) -> str | None:
    try:
        return _keyring().get_password(SERVICE, name) or None
    except Fail:
        raise
    except Exception as e:
        raise Fail(E_SETUP, f"Windows Credential Manager not readable ({type(e).__name__})")


def kset(name: str, value: str) -> None:
    _keyring().set_password(SERVICE, name, value)


def kdel(name: str) -> None:
    try:
        _keyring().delete_password(SERVICE, name)
    except Exception:
        pass


def _client(api_key: str, token: str | None = None):
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        raise Fail(E_SETUP, f"the kiteconnect package is not installed for {sys.executable}: run this script with .venv\\Scripts\\python.exe")
    return KiteConnect(api_key=api_key, access_token=token, timeout=20)


def _err(e: Exception) -> str:
    m = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return f"{type(e).__name__}: {m}"[:200] if m else type(e).__name__


def pasted_twice(value: str, what: str) -> None:
    """Typing is hidden, so a double paste (Ctrl+V plus right-click) goes unnoticed: refuse 'abcabc'."""
    n = len(value)
    if n >= 8 and n % 2 == 0 and value[: n // 2] == value[n // 2:]:
        raise Fail(E_SETUP, f"the {what} looks pasted twice (the same text repeated); nothing changed. "
                            "Run --setup again and paste it once")


def open_login_page(url: str) -> None:
    """Prefer Chrome (where the user is logged in to Kite); else the default browser. The redirect goes to
    127.0.0.1 on this PC, so any browser here works."""
    for p in (os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
              os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
              os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")):
        if os.path.isfile(p):
            try:
                subprocess.Popen([p, url])
                return
            except OSError:
                break
    webbrowser.open(url)


# ---------- --setup ----------
def cmd_setup() -> int:
    print("Stores your Kite Connect credentials in Windows Credential Manager (service papertrade-kite).")
    print("What you type is not shown. Press Enter on an optional field to leave it as it is.\n")
    have = bool(kget("api_key"))
    key = getpass.getpass("Kite API key" + (" (Enter = keep the stored one)" if have else "") + ": ").strip()
    pasted_twice(key, "API key")
    if key:
        kset("api_key", key)
    elif not have:
        raise Fail(E_SETUP, "an API key is required")
    have = bool(kget("api_secret"))
    sec = getpass.getpass("Kite API secret" + (" (Enter = keep the stored one)" if have else "") + ": ").strip()
    pasted_twice(sec, "API secret")
    if sec:
        kset("api_secret", sec)
    elif not have:
        raise Fail(E_SETUP, "an API secret is required")
    ans = input("\nAlso store your Zerodha user ID, password and TOTP secret for AUTOMATIC daily login? [y/N]: ")
    if ans.strip().lower().startswith("y"):
        uid = input("Zerodha user ID (client ID, e.g. AB1234): ").strip().upper()
        pwd = getpass.getpass("Zerodha password: ")
        totp = getpass.getpass("TOTP secret (the text key shown when you enable external TOTP): ").strip().replace(" ", "")
        if not (uid and pwd and totp):
            raise Fail(E_SETUP, "user ID, password and TOTP secret are all needed for automatic login; nothing changed for them")
        try:
            import pyotp
            pyotp.TOTP(totp).now()
        except ImportError:
            raise Fail(E_SETUP, f"the pyotp package is not installed for {sys.executable}: run this script with .venv\\Scripts\\python.exe")
        except Exception:
            raise Fail(E_SETUP, "that TOTP secret is not valid base32 text; nothing stored for automatic login")
        kset("user_id", uid)
        kset("password", pwd)
        kset("totp_secret", totp)
        say("Stored: API key, API secret, user ID, password, TOTP secret. Default login mode: automatic.")
    else:
        ans = input("Remove any stored user ID / password / TOTP secret (manual login only)? [y/N]: ")
        if ans.strip().lower().startswith("y"):
            for n in ("user_id", "password", "totp_secret"):
                kdel(n)
        say("Stored: API key and API secret. Default login mode: "
            + ("automatic" if kget("totp_secret") else "manual (browser)") + ".")
    print("Next: .venv\\Scripts\\python.exe tools\\kite_login.py --manual   (logs in and writes KITE_ACCESS_TOKEN to GitHub)")
    return OK


# ---------- request token: manual ----------
def manual_request_token(kite) -> str:
    got: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            u = urlparse(self.path)
            if u.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            q = parse_qs(u.query)
            got["status"] = (q.get("status") or [""])[0]
            got["request_token"] = (q.get("request_token") or [""])[0]
            ok = got["status"] == "success" and got["request_token"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<h3>PaperTrade: Kite login " + ("received. You can close this tab." if ok else
                              "did not succeed. Check the terminal.") + "</h3>").encode("utf-8"))
            done.set()

        def log_message(self, *a):          # the request line holds the request token: keep it out of logs
            pass

    try:
        srv = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    except OSError as e:
        raise Fail(E_LOGIN, f"cannot listen on {CALLBACK_HOST}:{CALLBACK_PORT} ({e.strerror}); is another login running?")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    say(f"Opening the Kite login page in your browser; waiting up to {MANUAL_TIMEOUT // 60} minutes for the redirect "
        f"to http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}")
    url = kite.login_url()
    print("If the page does not open in the browser where you are logged in to Kite, paste this into it:\n  " + url)
    open_login_page(url)
    ok = done.wait(MANUAL_TIMEOUT)
    srv.shutdown()
    srv.server_close()
    if not ok:
        raise Fail(E_LOGIN, "manual login timed out (no redirect received)")
    if got.get("status") != "success" or not got.get("request_token"):
        raise Fail(E_LOGIN, f"Kite redirected with status {got.get('status') or 'missing'} and no request token")
    return got["request_token"]


# ---------- request token: automatic (TOTP) ----------
def auto_request_token(kite) -> str:
    """Kite's web login, as the community scripts do it: POST /api/login (user ID, password) -> request_id,
    POST /api/twofa (TOTP) -> session cookies, then GET the Connect login URL and follow its redirects by
    hand until one carries request_token (the last hop points at 127.0.0.1:5555, which is not requested).
    Tolerated by Zerodha rather than supported: if it breaks, use --manual."""
    try:
        import pyotp
        import requests
    except ImportError as e:
        raise Fail(E_SETUP, f"{e.name} is not installed for {sys.executable}: run this script with .venv\\Scripts\\python.exe")
    uid, pwd, totp = kget("user_id"), kget("password"), kget("totp_secret")
    if not (uid and pwd and totp):
        raise Fail(E_SETUP, "automatic login needs the user ID, password and TOTP secret: run --setup and answer y")
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PaperTrade-kite-login"
    try:
        r = s.post(f"{KITE_WEB}/api/login", data={"user_id": uid, "password": pwd}, timeout=20)
        j = r.json() if "json" in r.headers.get("content-type", "") else {}
    except Exception as e:
        raise Fail(E_LOGIN, f"login step 1 (user ID + password) failed: {type(e).__name__}")
    if r.status_code != 200 or j.get("status") != "success":
        raise Fail(E_LOGIN, f"login step 1 (user ID + password) refused: HTTP {r.status_code}, "
                            f"{(j.get('message') or 'no message')[:120]}")
    request_id = (j.get("data") or {}).get("request_id")
    if not request_id:
        raise Fail(E_LOGIN, "login step 1 answered without a request_id (the page may have changed)")
    try:
        r = s.post(f"{KITE_WEB}/api/twofa", data={"user_id": uid, "request_id": request_id,
                                                  "twofa_value": pyotp.TOTP(totp).now(), "twofa_type": "totp",
                                                  "skip_totp": "true"}, timeout=20)
        j = r.json() if "json" in r.headers.get("content-type", "") else {}
    except Exception as e:
        raise Fail(E_LOGIN, f"login step 2 (TOTP) failed: {type(e).__name__}")
    if r.status_code != 200 or j.get("status") != "success":
        raise Fail(E_LOGIN, f"login step 2 (TOTP) refused: HTTP {r.status_code}, {(j.get('message') or 'no message')[:120]}"
                            " (check the PC clock and the stored TOTP secret)")
    url = kite.login_url() + "&skip_session=true"
    for _hop in range(12):
        if "request_token=" in url:
            break
        try:
            r = s.get(url, allow_redirects=False, timeout=20)
        except Exception as e:
            raise Fail(E_LOGIN, f"login step 3 (Connect redirect) failed: {type(e).__name__}")
        loc = r.headers.get("Location")
        if r.status_code not in (301, 302, 303, 307, 308) or not loc:
            raise Fail(E_LOGIN, f"login step 3 stopped at HTTP {r.status_code} without a request token. If this "
                                "is the first login with this app, run --manual once and click Authorize.")
        url = urljoin(url, loc)
    q = parse_qs(urlparse(url).query)
    tok = (q.get("request_token") or [""])[0]
    if not tok:
        raise Fail(E_LOGIN, "login step 3 never reached the redirect with a request token")
    return tok


# ---------- the token ----------
def token_works(api_key: str, token: str) -> tuple[bool, str]:
    try:
        _client(api_key, token).profile()
        return True, ""
    except Fail:
        raise
    except Exception as e:
        return False, _err(e)


def _gh() -> str | None:
    g = shutil.which("gh")
    if g:
        return g
    for p in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "GitHub CLI" / "gh.exe",
              # a portable copy; Task Scheduler does not get the terminal's PATH, so which() misses it there
              Path.home() / "tools" / "gh" / "bin" / "gh.exe"):
        if p.is_file():
            return str(p)
    return None


def write_github_secret(token: str) -> None:
    gh = _gh()
    if not gh:
        raise Fail(E_GH, "the GitHub CLI (gh) is not installed: winget install GitHub.cli, then gh auth login")
    # No --body: gh then reads the value from stdin (not a terminal here), so the token is never on a
    # command line or in the process list.
    r = subprocess.run([gh, "secret", "set", SECRET_NAME, "--repo", REPO], input=token, text=True,
                       capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # no console flash from the task
    if r.returncode != 0:
        raise Fail(E_GH, f"gh secret set failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:200]}"
                         " - is gh logged in? (gh auth status)")


SCHED_FROM, SCHED_TO = (7, 0), (18, 30)     # --scheduled logs in only Monday-Friday inside this IST window
MANUAL_RETRY_MIN = 55                       # --scheduled, manual mode: one browser page per this many minutes


def in_schedule_window(now: datetime) -> bool:
    """Monday-Friday, SCHED_FROM <= time < SCHED_TO (IST). The token dies about 06:00 and is first used by
    the evening run, so a login outside this window is of no use. NSE holidays are not known here: a token
    made on one is harmless."""
    return now.weekday() < 5 and SCHED_FROM <= (now.hour, now.minute) < SCHED_TO


def cmd_login(mode: str, if_needed: bool, scheduled: bool = False) -> int:
    api_key, api_secret = kget("api_key"), kget("api_secret")
    if not (api_key and api_secret):
        raise Fail(E_SETUP, "no credentials stored: run .venv\\Scripts\\python.exe tools\\kite_login.py --setup")
    now = datetime.now(IST)
    if scheduled and not in_schedule_window(now):
        say(f"Scheduled run outside Monday-Friday {SCHED_FROM[0]:02d}:{SCHED_FROM[1]:02d}-"
            f"{SCHED_TO[0]:02d}:{SCHED_TO[1]:02d} IST; nothing to do.")
        return OK
    if if_needed and kget("access_token_date") == today_ist() and kget("access_token"):
        ok, why = token_works(api_key, kget("access_token"))
        if ok:
            if kget("github_secret_date") != today_ist():
                # an earlier run made the token but could not write it to GitHub (exit 4): finish that now
                write_github_secret(kget("access_token"))
                kset("github_secret_date", today_ist())
                say(f"Today's token already works; GitHub secret {SECRET_NAME} updated on {REPO}.")
                return OK
            say("Today's token already works; nothing to do.")
            return OK
        if why.split(":")[0] in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "SSLError",
                                 "NetworkException"):
            # no network yet (the task also fires the moment a network appears): the token was not refused,
            # so do not log in again; the next run checks it
            say(f"Could not reach Kite to check today's token ({why[:80]}); leaving it as it is.")
            return OK
        say(f"Today's stored token does not work ({why}); logging in again.")
    if mode == "default":
        mode = "auto" if (kget("totp_secret") and kget("user_id") and kget("password")) else "manual"
    if scheduled and mode == "manual":
        last = kget("manual_attempt_at")
        try:
            mins = (now - datetime.fromisoformat(last)).total_seconds() / 60 if last else None
        except ValueError:
            mins = None
        if mins is not None and 0 <= mins < MANUAL_RETRY_MIN:
            say(f"The Kite login page was opened {mins:.0f} minutes ago; not opening it again yet.")
            return OK
        kset("manual_attempt_at", now.isoformat(timespec="seconds"))
    say(f"Logging in ({mode}).")
    kite = _client(api_key)
    req = auto_request_token(kite) if mode == "auto" else manual_request_token(kite)
    log.info("request token received")
    try:
        sess = kite.generate_session(req, api_secret=api_secret)
        token = sess["access_token"]
    except Exception as e:
        raise Fail(E_EXCHANGE, f"token exchange failed: {_err(e)}")
    del sess                                       # holds the token and account data: never logged
    kset("access_token", token)
    kset("access_token_date", today_ist())
    ok, why = token_works(api_key, token)
    if not ok:
        raise Fail(E_EXCHANGE, f"the new token does not work: {why}")
    say(f"Access token for {today_ist()} made and stored in Credential Manager.")
    write_github_secret(token)
    kset("github_secret_date", today_ist())
    say(f"GitHub secret {SECRET_NAME} updated on {REPO}.")
    return OK


# ---------- --check ----------
def cmd_check() -> int:
    api_key = kget("api_key")
    if not api_key:
        print("Kite: no credentials stored (run: .venv\\Scripts\\python.exe tools\\kite_login.py --setup)")
        return E_SETUP
    tok, day = kget("access_token"), kget("access_token_date")
    if not tok or day != today_ist():
        print(f"Kite: no token for today ({today_ist()})" + (f"; the stored one is from {day}" if tok and day else ""))
        return E_CHECK
    print(f"Kite: token for today ({today_ist()}) exists")
    ok, why = token_works(api_key, tok)
    print("Kite: profile() works" if ok else f"Kite: profile() failed ({why})")
    return OK if ok else E_CHECK


# ---------- --install-task ----------
def _task_xml(python: str, script: str) -> str:
    user = os.environ.get("USERDOMAIN", "") + "\\" + getpass.getuser() if os.environ.get("USERDOMAIN") else getpass.getuser()
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    net = ('<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational">'
           '<Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[(EventID=10000)]]</Select>'
           '</Query></QueryList>')
    triggers = (
        # every hour 09:00 ... 18:00, Monday-Friday
        "<CalendarTrigger><Repetition><Interval>PT1H</Interval><Duration>PT9H30M</Duration>"
        "<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
        "<StartBoundary>2026-01-01T09:00:00</StartBoundary><Enabled>true</Enabled>"
        "<ScheduleByWeek><DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek>"
        "<WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>"
        # sign-in and unlock (a minute later, so the Wi-Fi has time to connect)
        f"<LogonTrigger><Enabled>true</Enabled><UserId>{esc(user)}</UserId><Delay>PT1M</Delay></LogonTrigger>"
        f"<SessionStateChangeTrigger><Enabled>true</Enabled><StateChange>SessionUnlock</StateChange>"
        f"<UserId>{esc(user)}</UserId><Delay>PT1M</Delay></SessionStateChangeTrigger>"
        # a network connects (Wi-Fi switched back on)
        f"<EventTrigger><Enabled>true</Enabled><Subscription>{esc(net)}</Subscription><Delay>PT30S</Delay></EventTrigger>")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>PaperTrade: daily Zerodha Kite Connect login (data only), writes the GitHub secret KITE_ACCESS_TOKEN. Runs at sign-in, unlock, network connect and hourly 09:00-18:00 on weekdays; does nothing once today's token works or outside Monday-Friday 07:00-18:30.</Description></RegistrationInfo>
  <Triggers>{triggers}</Triggers>
  <Principals><Principal id="Author"><UserId>{esc(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{esc(python)}</Command><Arguments>"{esc(script)}" --scheduled</Arguments><WorkingDirectory>{esc(str(ROOT))}</WorkingDirectory></Exec></Actions>
</Task>
"""


def cmd_install_task() -> int:
    if os.name != "nt":
        raise Fail(E_TASK, "--install-task is for Windows Task Scheduler")
    # pythonw.exe has no console, so the task shows no black window (it runs at every unlock and every hour);
    # print() is then a no-op and everything the run says is in tools/kite_login.log
    venv = ROOT / ".venv" / "Scripts"
    python = str(next((p for p in (venv / "pythonw.exe", venv / "python.exe") if p.is_file()), Path(sys.executable)))
    xml = _task_xml(python, str(Path(__file__).resolve()))
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(tmp).write_text(xml, encoding="utf-16")
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp, "/F"], capture_output=True, text=True)
    finally:
        Path(tmp).unlink(missing_ok=True)
    if r.returncode != 0:
        raise Fail(E_TASK, f"schtasks failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    say(f'Task "{TASK_NAME}" registered: at sign-in, at unlock, when a network connects, and every hour '
        f"09:00-18:00 Monday-Friday (this PC's local time); runs {python} tools\\kite_login.py --scheduled as you, "
        f"only while you are logged on. It logs in only Monday-Friday 07:00-18:30 and only if today's token is "
        f"missing. Check it in Task Scheduler; "
        f'run it now with: schtasks /Run /TN "{TASK_NAME}"')
    return OK


def main(argv=None) -> int:
    _setup_logging()
    p = argparse.ArgumentParser(prog="kite_login.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--setup", action="store_true")
    g.add_argument("--manual", action="store_true")
    g.add_argument("--auto", action="store_true")
    g.add_argument("--install-task", action="store_true")
    g.add_argument("--check", action="store_true")
    p.add_argument("--if-needed", action="store_true")
    p.add_argument("--scheduled", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.setup:
            return cmd_setup()
        if a.check:
            return cmd_check()
        if a.install_task:
            return cmd_install_task()
        return cmd_login("manual" if a.manual else "auto" if a.auto else "default", a.if_needed or a.scheduled, a.scheduled)
    except Fail as f:
        print(f"FAILED: {f}", file=sys.stderr)
        log.error("exit %s: %s", f.code, f)
        return f.code
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return E_SETUP


if __name__ == "__main__":
    sys.exit(main())
