from __future__ import annotations
import asyncio
import base64
import html
import hashlib
import json
import logging
import os
import random
import re
import secrets
import socket
import string
import subprocess
import sys
import threading
import time

OTP_PATTERN = re.compile(r"\b(\d{6})\b")
FORM_RE = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.I | re.S)
INPUT_RE = re.compile(r"<input\b(?P<attrs>[^>]*)>", re.I | re.S)
BUTTON_RE = re.compile(r"<button\b(?P<attrs>[^>]*)>(?P<body>.*?)</button>", re.I | re.S)
IFRAME_RE = re.compile(r"<iframe\b[^>]*\bsrc=(['\"])(?P<src>.*?)\1", re.I | re.S)
ATTR_RE = re.compile(r"([:\w-]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?")
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, unquote

import httpx
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import shutil

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
CALLBACK_PORT = 1455
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_ENDPOINT = "https://auth.openai.com/oauth/authorize"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"
DEFAULT_TEMPMAIL_BASE_URL = "https://api.tempmail.lol/v2"
MAILTM_BASE_URL = "https://api.mail.tm"
DEFAULT_EMAIL_PROVIDERS = ("tempmail", "mailtm")
DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
DEFAULT_BROWSER_LOCALE = "en-US"
DEFAULT_BROWSER_TIMEZONE = "America/New_York"
DEFAULT_BROWSER_VIEWPORT = {"width": 1365, "height": 900}
DEFAULT_LOGIN_CHALLENGE_TIMEOUT = 90


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Missing config file: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)

    for key, default in [
        ("token_dir", "tokens"),
        ("log_dir", "logs"),
        ("virtualbrowser_profile_root", "profiles"),
    ]:
        value = config.get(key, default)
        if not os.path.isabs(value):
            config[key] = str((SCRIPT_DIR / value).resolve())

    executable = config.get("virtualbrowser_executable_path")
    if executable and not os.path.isabs(executable):
        config["virtualbrowser_executable_path"] = str((SCRIPT_DIR / executable).resolve())

    return config


cfg = load_config()
TOKEN_DIR = cfg["token_dir"]
LOG_DIR = cfg["log_dir"]
RUN_COUNT = int(cfg.get("run_count", 1))
RUN_INTERVAL = int(cfg.get("run_interval", 60))
MIN_INTERVAL = int(cfg.get("min_interval", RUN_INTERVAL))
MAX_INTERVAL = int(cfg.get("max_interval", RUN_INTERVAL))
CONCURRENCY = max(1, int(cfg.get("concurrency", 1)))
HEADLESS = bool(cfg.get("headless", False))
API_PROXY = cfg.get("api_proxy") or None
BROWSER_PROXY = cfg.get("browser_proxy") or None
BROWSER_USER_AGENT = str(cfg.get("browser_user_agent", DEFAULT_BROWSER_USER_AGENT)).strip()
BROWSER_LOCALE = str(cfg.get("browser_locale", DEFAULT_BROWSER_LOCALE)).strip() or DEFAULT_BROWSER_LOCALE
BROWSER_TIMEZONE = str(cfg.get("browser_timezone", DEFAULT_BROWSER_TIMEZONE)).strip() or DEFAULT_BROWSER_TIMEZONE
BROWSER_VIEWPORT = {
    "width": int(cfg.get("browser_width", DEFAULT_BROWSER_VIEWPORT["width"])),
    "height": int(cfg.get("browser_height", DEFAULT_BROWSER_VIEWPORT["height"])),
}
LOGIN_CHALLENGE_TIMEOUT = max(15, int(cfg.get("login_challenge_timeout", DEFAULT_LOGIN_CHALLENGE_TIMEOUT)))
LOG_ENABLED = bool(cfg.get("log_enabled", False))
ACCOUNT_PASSWORD = cfg.get("account_password", "")
TEMPMAIL_BASE_URL = cfg.get("tempmail_base_url", DEFAULT_TEMPMAIL_BASE_URL).rstrip("/")
TEMPMAIL_TIMEOUT = int(cfg.get("tempmail_timeout", 120))
TEMPMAIL_API_KEY = str(cfg.get("tempmail_api_key", "")).strip()
VB_EXE = cfg.get("virtualbrowser_executable_path") or None
VB_PROFILE_ROOT = cfg.get("virtualbrowser_profile_root", str((SCRIPT_DIR / "profiles").resolve()))
WORKER_ID_MIN = int(cfg.get("worker_id_min", 1))
WORKER_ID_MAX = int(cfg.get("worker_id_max", 1000))
if WORKER_ID_MIN > WORKER_ID_MAX:
    WORKER_ID_MIN, WORKER_ID_MAX = WORKER_ID_MAX, WORKER_ID_MIN
if MIN_INTERVAL > MAX_INTERVAL:
    MIN_INTERVAL, MAX_INTERVAL = MAX_INTERVAL, MIN_INTERVAL

PROXY_PORT = int(cfg.get("proxy_port", 7897))
PROXY_GROUP = str(cfg.get("proxy_group", "Proxy")).strip() or "Proxy"
PROXY_TEST_URL = str(cfg.get("proxy_test_url", "https://chatgpt.com")).strip() or "https://chatgpt.com"
PROXY_TEST_TIMEOUT = int(cfg.get("proxy_test_timeout", 10))
PROXY_ROTATION_STATE_FILE = cfg.get("proxy_rotation_state_file", "./proxy_rotation_state.json")

raw_email_providers = cfg.get("email_providers")
if isinstance(raw_email_providers, (list, tuple)):
    EMAIL_PROVIDERS = tuple(
        str(provider).strip().lower()
        for provider in raw_email_providers
        if isinstance(provider, str) and provider.strip()
    )
else:
    EMAIL_PROVIDERS = DEFAULT_EMAIL_PROVIDERS

if not EMAIL_PROVIDERS:
    EMAIL_PROVIDERS = DEFAULT_EMAIL_PROVIDERS

# OAuth global state
OAUTH_LOOP = None
state_waiters: dict = {}
state_results: dict = {}
state_waiters_lock = threading.Lock()


def rotate_proxy_on_failure() -> bool:
    """Call rotate_proxy.py to switch to next available node. Returns True if successful."""
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "rotate_proxy.py")],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Failed to rotate proxy: {e}")
        return False


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("vb_register")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    if LOG_ENABLED:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        print(f"Log file: {path}")
    else:
        logger.addHandler(logging.NullHandler())

    return logger


def build_tempmail_headers(content_type: str | None = "application/json") -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    if TEMPMAIL_API_KEY:
        headers["X-API-Key"] = TEMPMAIL_API_KEY
        headers.setdefault("Authorization", f"Bearer {TEMPMAIL_API_KEY}")
    return headers


log = setup_logging()


def detect_virtualbrowser_exe() -> str | None:
    candidates: list[Path] = []

    for root in [
        Path(r"D:\Program Files\VirtualBrowser\VirtualBrowser"),
        Path(r"C:\Program Files\VirtualBrowser\VirtualBrowser"),
    ]:
        if root.exists():
            candidates.extend(sorted(root.glob(r"*\VirtualBrowser.exe"), reverse=True))

    candidates.extend(
        [
            Path(r"D:\VirtualBrowser\Chrome-bin\VirtualBrowser.exe"),
            Path(r"D:\Program Files\VirtualBrowser\VirtualBrowser.exe"),
            Path(r"C:\Program Files\VirtualBrowser\VirtualBrowser.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "VirtualBrowser" / "Chrome-bin" / "VirtualBrowser.exe",
        ]
    )

    for path in candidates:
        if path.exists():
            return str(path)
    return None


def resolve_virtualbrowser_exe() -> str:
    if VB_EXE:
        if os.path.exists(VB_EXE):
            return VB_EXE
        raise FileNotFoundError(f"VirtualBrowser.exe not found: {VB_EXE}")

    detected = detect_virtualbrowser_exe()
    if detected:
        return detected

    raise FileNotFoundError("VirtualBrowser.exe not found, set virtualbrowser_executable_path in config.json")


def generate_pkce_codes() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def build_auth_url(challenge: str, state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "openid email profile offline_access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def notify_oauth_result(state: str, data: dict) -> None:
    loop = OAUTH_LOOP
    if not state:
        return
    with state_waiters_lock:
        future = state_waiters.pop(state, None)
        if future is None:
            state_results[state] = data
            return
    if not loop:
        with state_waiters_lock:
            state_results[state] = data
        return
    if future and not future.done():
        loop.call_soon_threadsafe(future.set_result, data)


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/auth/callback":
            query = parse_qs(parsed.query)
            result = {
                "code": query.get("code", [None])[0],
                "state": query.get("state", [None])[0],
                "error": query.get("error", [None])[0],
            }
            print(
                f"[oauth_callback] received host={self.headers.get('Host', '')} "
                f"state={result.get('state')} has_code={bool(result.get('code'))} error={result.get('error')}"
            )
            notify_oauth_result(result.get("state"), result)
            self.send_response(302)
            self.send_header("Location", "/success")
            self.end_headers()
            return

        if parsed.path == "/success":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization succeeded</h1><p>You can close this page.</p>")
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args) -> None:
        return


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


class ReusableIPv6HTTPServer(ReusableHTTPServer):
    address_family = socket.AF_INET6


def start_oauth_server(loop: asyncio.AbstractEventLoop) -> list[HTTPServer]:
    global OAUTH_LOOP
    OAUTH_LOOP = loop
    servers: list[HTTPServer] = []
    candidates = [
        (ReusableHTTPServer, ("127.0.0.1", CALLBACK_PORT), "ipv4"),
        (ReusableIPv6HTTPServer, ("::1", CALLBACK_PORT), "ipv6"),
    ]
    for server_cls, address, label in candidates:
        try:
            server = server_cls(address, OAuthCallbackHandler)
        except Exception as exc:
            print(f"[oauth_callback] listen {label} failed: {exc}")
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        print(f"[oauth_callback] listening on {address[0]}:{CALLBACK_PORT} ({label})")
    if not servers:
        raise RuntimeError(f"Unable to start OAuth callback server on port {CALLBACK_PORT}")
    return servers


async def wait_for_oauth_result(state: str, timeout: int = 20) -> dict | None:
    if not state:
        return None
    loop = OAUTH_LOOP
    if loop is None:
        raise RuntimeError("OAuth loop not initialized")

    future = loop.create_future()
    with state_waiters_lock:
        existing = state_results.pop(state, None)
        if existing is not None:
            return existing
        state_waiters[state] = future
    try:
        return await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        with state_waiters_lock:
            state_waiters.pop(state, None)
        if not future.done():
            future.cancel()


def build_httpx_client(timeout: int = 30) -> httpx.AsyncClient:
    kwargs = {"timeout": timeout}
    if API_PROXY:
        kwargs["proxy"] = API_PROXY
    return httpx.AsyncClient(**kwargs)


def extract_proxy_server(proxy_value: str | None) -> str | None:
    """Extract just the server part (scheme://host:port) from a proxy URL, without credentials."""
    if not proxy_value:
        return None

    parsed = urlparse(proxy_value)
    if not parsed.scheme or not parsed.hostname:
        return proxy_value

    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    return server


def build_playwright_proxy(proxy_value: str | None) -> dict | None:
    if not proxy_value:
        return None

    parsed = urlparse(proxy_value)
    if not parsed.scheme or not parsed.hostname:
        return {"server": proxy_value, "bypass": "localhost,127.0.0.1,::1"}

    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    proxy: dict[str, str] = {"server": server, "bypass": "localhost,127.0.0.1,::1"}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


async def exchange_code_for_tokens(code: str, verifier: str) -> dict | None:
    async with build_httpx_client(timeout=30) as client:
        resp = await client.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    return resp.json() if resp.status_code == 200 else None


def parse_account_id_from_jwt(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    auth_payload = payload.get("https://api.openai.com/auth") if isinstance(payload.get("https://api.openai.com/auth"), dict) else {}
    return str(auth_payload.get("chatgpt_account_id") or "").strip()


def decode_jwt_payload(access_token: str) -> dict:
    try:
        payload_b64 = access_token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        print(f"[jwt] decode failed: {exc}")
        return {}


def parse_token_expiry_from_jwt(access_token: str) -> datetime | None:
    payload = decode_jwt_payload(access_token)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except Exception:
        return None


def save_tokens(email: str, token_data: dict) -> str:
    os.makedirs(TOKEN_DIR, exist_ok=True)
    path = os.path.join(TOKEN_DIR, f"codex-{email}-free.json")
    now = datetime.now().astimezone()
    expires = now + timedelta(seconds=token_data.get("expires_in", 864000))
    account_id = (
        str(token_data.get("account_id") or "").strip()
        or str(token_data.get("session_account_id") or "").strip()
        or parse_account_id_from_jwt(token_data.get("access_token", ""))
    )

    def fmt(dt: datetime) -> str:
        text = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        return text[:-2] + ":" + text[-2:]

    payload = {
        "access_token": token_data.get("access_token", ""),
        "account_id": account_id,
        "account_password": token_data.get("account_password", ""),
        "mail_token": token_data.get("mail_token", ""),
        "disabled": False,
        "email": email,
        "expired": fmt(expires),
        "id_token": token_data.get("id_token", ""),
        "last_refresh": fmt(now),
        "refresh_token": token_data.get("refresh_token", ""),
        "type": "codex",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=4, ensure_ascii=False)
    return path


def build_password() -> str:
    if ACCOUNT_PASSWORD:
        return str(ACCOUNT_PASSWORD)
    alphabet = string.ascii_letters + string.digits
    return "Aa!" + "".join(random.choices(alphabet, k=10)) + "9"


async def create_tempmail_email(max_retries: int = 6) -> dict:
    for attempt in range(1, max_retries + 1):
        async with build_httpx_client(timeout=30) as client:
            resp = await client.post(
                f"{TEMPMAIL_BASE_URL}/inbox/create",
                headers=build_tempmail_headers("application/json"),
                json={},
            )

        if resp.status_code in (200, 201):
            data = resp.json()
            email = str(data.get("address", "")).strip()
            token = str(data.get("token", "")).strip()
            if not email or not token:
                raise RuntimeError(f"Tempmail create returned incomplete data: {data}")
            return {"email": email, "token": token}

        if resp.status_code == 429:
            wait = 5 + attempt * 2
            print(f"Tempmail rate limited, retrying after {wait}s (attempt {attempt})")
            await asyncio.sleep(wait)
            continue

        raise RuntimeError(f"Tempmail create failed: HTTP {resp.status_code} {resp.text[:200]}")

    raise RuntimeError("Tempmail create failed after multiple retries due to rate limiting")


def build_tempmail_message_id(msg: dict) -> str:
    msg_id = str(msg.get("date") or msg.get("id") or "")
    if not msg_id:
        msg_id = json.dumps(msg, sort_keys=True, ensure_ascii=False)
    return msg_id


async def get_tempmail_message_ids(token: str) -> set[str]:
    async with build_httpx_client(timeout=15) as client:
        try:
            resp = await client.get(
                f"{TEMPMAIL_BASE_URL}/inbox",
                params={"token": token},
                headers=build_tempmail_headers(None),
            )
        except Exception:
            return set()

    if resp.status_code != 200:
        return set()

    data = resp.json()
    emails = data.get("emails", []) if isinstance(data, dict) else []
    if not isinstance(emails, list):
        return set()

    ids: set[str] = set()
    for msg in emails:
        if isinstance(msg, dict):
            ids.add(build_tempmail_message_id(msg))
    return ids


async def get_tempmail_verification_code(
    email: str, token: str, timeout: int = 120, seen_ids: set[str] | None = None
) -> str | None:
    start = time.time()
    seen_ids = set(seen_ids or ())

    async with build_httpx_client(timeout=15) as client:
        while time.time() - start < timeout:
            try:
                resp = await client.get(
                    f"{TEMPMAIL_BASE_URL}/inbox",
                    params={"token": token},
                    headers=build_tempmail_headers(None),
                )
                if resp.status_code != 200:
                    await asyncio.sleep(3)
                    continue

                data = resp.json()
                emails = data.get("emails", []) if isinstance(data, dict) else []
                if not isinstance(emails, list):
                    await asyncio.sleep(3)
                    continue

                for msg in emails:
                    if not isinstance(msg, dict):
                        continue

                    msg_id = build_tempmail_message_id(msg)
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    sender = str(msg.get("from", "")).lower()
                    subject = str(msg.get("subject", ""))
                    body = str(msg.get("body", ""))
                    html = str(msg.get("html") or "")
                    content = "\n".join([sender, subject, body, html])

                    if "openai" not in sender and "openai" not in content.lower():
                        continue

                    match = OTP_PATTERN.search(content)
                    if match:
                        return match.group(1)
            except Exception as exc:
                log.debug(f"检查 tempmail 收件箱失败: {exc}")

            await asyncio.sleep(3)

    return None


async def fetch_mailtm_domain(client: httpx.AsyncClient) -> str:
    resp = await client.get(f"{MAILTM_BASE_URL}/domains", timeout=20)
    resp.raise_for_status()
    data = resp.json()
    members = data.get("hydra:member") or []
    for entry in members:
        if isinstance(entry, dict):
            domain = entry.get("domain")
            if domain:
                return domain
    raise RuntimeError("Mail.tm returned no domains")


async def create_mailtm_email() -> dict:
    async with build_httpx_client(timeout=30) as client:
        domain = await fetch_mailtm_domain(client)
        address = f"mailtm{secrets.token_hex(8)}@{domain}"
        password = build_password()
        resp = await client.post(
            f"{MAILTM_BASE_URL}/accounts",
            json={"address": address, "password": password},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Mail.tm account creation failed: {resp.status_code} {resp.text}")
        token_resp = await client.post(
            f"{MAILTM_BASE_URL}/token",
            json={"address": address, "password": password},
            timeout=30,
        )
        token_resp.raise_for_status()
        data = token_resp.json()
        token = data.get("token")
        if not token:
            raise RuntimeError("Mail.tm token response missing token")
        return {"email": address, "token": token}


async def get_mailtm_message_ids(token: str) -> set[str]:
    if not token:
        return set()
    headers = {"Authorization": f"Bearer {token}"}
    async with build_httpx_client(timeout=15) as client:
        resp = await client.get(f"{MAILTM_BASE_URL}/messages", headers=headers, timeout=15)
        if resp.status_code != 200:
            return set()
        data = resp.json()
        items = data.get("hydra:member") or []
        return {item.get("id") for item in items if isinstance(item, dict) and item.get("id")}


async def get_mailtm_verification_code(
    token: str, timeout: int = 120, seen_ids: set[str] | None = None
) -> str | None:
    if not token:
        return None
    start = time.time()
    seen_ids = set(seen_ids or ())
    headers = {"Authorization": f"Bearer {token}"}

    async with build_httpx_client(timeout=20) as client:
        while time.time() - start < timeout:
            try:
                resp = await client.get(f"{MAILTM_BASE_URL}/messages", headers=headers, timeout=15)
                if resp.status_code != 200:
                    await asyncio.sleep(3)
                    continue
                data = resp.json()
                items = data.get("hydra:member") or []
                for msg in items:
                    if not isinstance(msg, dict):
                        continue
                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    parts = []
                    sender = msg.get("from")
                    if isinstance(sender, dict):
                        parts.append(str(sender.get("address", "")))
                        parts.append(str(sender.get("name", "")))
                    else:
                        parts.append(str(sender))
                    for field in ("subject", "intro", "text", "html"):
                        value = msg.get(field) or ""
                        parts.append(str(value))
                    content = "\n".join(part for part in parts if part)
                    if "openai" not in content.lower():
                        continue
                    match = OTP_PATTERN.search(content)
                    if match:
                        return match.group(1)
            except Exception as exc:
                log.debug(f"检查 Mail.tm 收件箱失败: {exc}")
            await asyncio.sleep(3)
    return None


async def allocate_temporary_email() -> dict | None:
    last_exc: Exception | None = None
    for provider in EMAIL_PROVIDERS:
        try:
            if provider == "tempmail":
                info = await create_tempmail_email()
            elif provider == "mailtm":
                info = await create_mailtm_email()
            else:
                log.warning("Unknown email provider configured: %s", provider)
                continue
            info["provider"] = provider
            return info
        except Exception as exc:
            log.warning("Email provider %s failed: %s", provider, exc)
            last_exc = exc
    if last_exc:
        log.error("所有临时邮箱提供商都失败了: %s", last_exc)
    return None


def provider_label(name: str) -> str:
    return {
        "tempmail": "Tempmail.lol",
        "mailtm": "Mail.tm",
    }.get(name, name)


async def get_message_ids_for_provider(provider: str, token: str) -> set[str]:
    if provider == "tempmail":
        return await get_tempmail_message_ids(token)
    if provider == "mailtm":
        return await get_mailtm_message_ids(token)
    return set()


async def get_verification_code_for_provider(
    provider: str, email: str, token: str, timeout: int, seen_ids: set[str] | None = None
) -> str | None:
    if provider == "tempmail":
        return await get_tempmail_verification_code(email, token, timeout, seen_ids)
    if provider == "mailtm":
        return await get_mailtm_verification_code(token, timeout, seen_ids)
    return None


async def type_slowly(page, locator, text: str) -> None:
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass

    focused = False
    for focus_action in (
        lambda: locator.click(timeout=3000),
        lambda: locator.click(timeout=3000, force=True),
        lambda: locator.focus(),
    ):
        try:
            await focus_action()
            focused = True
            break
        except Exception:
            pass

    for clear_action in (
        lambda: locator.clear(),
        lambda: locator.fill(""),
    ):
        try:
            await clear_action()
            break
        except Exception:
            pass

    try:
        await locator.press_sequentially(text, delay=random.randint(30, 80))
        await page.wait_for_timeout(random.randint(10, 40))
        return
    except Exception:
        pass

    if focused:
        try:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
        except Exception:
            pass

    await locator.fill(text)


async def safe_click(page, locator) -> None:
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass

    for click_action in (
        lambda: locator.click(timeout=5000),
        lambda: locator.click(timeout=5000, force=True),
        lambda: locator.press("Enter"),
        lambda: locator.evaluate("(el) => el.click()"),
    ):
        try:
            await click_action()
            return
        except Exception:
            pass

    await locator.focus()
    await page.keyboard.press("Enter")


async def wait_after_input(page, min_ms: int = 1200, max_ms: int = 2600) -> None:
    await page.wait_for_timeout(random.randint(min_ms, max_ms))


async def pick(page, builders, timeout_ms: int = 10000):
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        for build in builders:
            loc = build(page)
            try:
                if await loc.count() > 0:
                    return loc.first
            except Exception:
                pass
        await page.wait_for_timeout(300)
    return None


async def page_title_safe(page) -> str:
    try:
        return await page.title()
    except Exception:
        return ""


async def page_body_safe(page, limit: int = 400) -> str:
    try:
        body = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        return ""
    return body[:limit].strip()


async def wait_for_login_ready(page, timeout_ms: int = LOGIN_CHALLENGE_TIMEOUT * 1000) -> None:
    end = time.time() + timeout_ms / 1000
    last_state = None
    selectors = [
        'input[type="email"]',
        'input[name="email"]',
        'button:has-text("Continue")',
        'button:has-text("Sign up")',
        'a:has-text("Sign up")',
    ]

    # First, wait for the page to have some content
    print("[登录页等待] 等待页面内容加载...")
    while time.time() < end:
        body = await page_body_safe(page)
        if body and len(body) > 100:
            print(f"[登录页等待] 页面内容已加载 ({len(body)} bytes)")
            break
        await page.wait_for_timeout(1000)

    # Then wait for interactive elements
    while time.time() < end:
        for selector in selectors:
            try:
                if await page.locator(selector).count() > 0:
                    return
            except Exception:
                pass

        title = await page_title_safe(page)
        body = await page_body_safe(page)
        state = f"{page.url}|{title}|{body[:120]}"
        if state != last_state:
            print(f"[登录页等待] URL: {page.url}")
            print(f"[登录页等待] Title: {title or '<empty>'}")
            if body:
                print(f"[登录页等待] Body: {body}")
            last_state = state

        await page.wait_for_timeout(2000)


async def fill_birthday_fields(page, birth_month: str, birth_day: str, birth_year: str) -> None:
    try:
        inputs = await page.locator("input").evaluate_all(
            """els => els.map((el, index) => ({
                index,
                name: el.name || "",
                placeholder: el.placeholder || "",
                ariaLabel: el.getAttribute("aria-label") || "",
                type: el.type || "",
                maxLength: Number(el.maxLength || 0),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                disabled: !!el.disabled,
                value: el.value || ""
            }))"""
        )
    except Exception:
        inputs = []

    visible_inputs = [
        item
        for item in inputs
        if item.get("visible") and not item.get("disabled") and item.get("type") != "hidden"
    ]
    print(
        "[生日字段] 可见输入框: "
        + "; ".join(
            f"#{item.get('index')} type={item.get('type')} name={item.get('name')} "
            f"placeholder={item.get('placeholder')} aria={item.get('ariaLabel')} "
            f"max={item.get('maxLength')} value={item.get('value')}"
            for item in visible_inputs
        )
    )

    def find_input(patterns: tuple[str, ...]) -> dict | None:
        for item in visible_inputs:
            text = " ".join(
                [
                    str(item.get("name", "")),
                    str(item.get("placeholder", "")),
                    str(item.get("ariaLabel", "")),
                ]
            ).lower()
            if any(pattern in text for pattern in patterns):
                return item
        return None

    month_meta = find_input(("month", "mm"))
    day_meta = find_input(("day", "dd"))
    year_meta = find_input(("year", "yyyy", "yy"))
    composite_meta = find_input(("birthday", "birth", "date of birth", "mm/dd", "yyyy"))
    age_meta = find_input(("age",))

    text_inputs = [
        item
        for item in visible_inputs
        if item.get("type") in ("", "text", "tel", "number", "date")
    ]

    # 处理 React Aria DateField（contenteditable spinbutton，无普通 input）
    try:
        year_seg = page.locator('[data-type="year"][contenteditable="true"]')
        month_seg = page.locator('[data-type="month"][contenteditable="true"]')
        day_seg = page.locator('[data-type="day"][contenteditable="true"]')
        if await year_seg.count() > 0 and await month_seg.count() > 0 and await day_seg.count() > 0:
            for seg, value in [(year_seg, birth_year), (month_seg, birth_month), (day_seg, birth_day)]:
                await seg.click()
                await page.keyboard.press("Control+a")
                await page.keyboard.type(value, delay=random.randint(50, 100))
                await page.wait_for_timeout(random.randint(100, 200))
            print(f"[生日字段] React Aria spinbutton 填写完成: {birth_year}/{birth_month}/{birth_day}")
            return
    except Exception as _e:
        print(f"[生日字段] React Aria spinbutton 尝试失败: {_e}")

    if age_meta:
        age_input = page.locator("input").nth(int(age_meta["index"]))
        age_value = str(max(21, datetime.now().year - int(birth_year)))
        try:
            await age_input.click(timeout=3000)
        except Exception:
            pass
        try:
            await age_input.fill(age_value)
        except Exception:
            pass
        try:
            await page.evaluate(
                """({index, value}) => {
                    const el = document.querySelectorAll('input')[index];
                    if (!el) return "";
                    el.focus();
                    el.value = value;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return el.value;
                }""",
                {"index": int(age_meta["index"]), "value": age_value},
            )
        except Exception:
            pass
        try:
            current_value = await age_input.input_value()
        except Exception:
            current_value = ""
        print(f"[年龄字段] target={age_value} current={current_value}")
        return

    if composite_meta is None and len(text_inputs) == 2:
        composite_meta = text_inputs[1]

    if composite_meta:
        composite_input = page.locator("input").nth(int(composite_meta["index"]))
        await type_slowly(page, composite_input, f"{birth_month}/{birth_day}/{birth_year}")
        return

    if not (month_meta and day_meta and year_meta) and len(text_inputs) >= 4:
        month_meta, day_meta, year_meta = text_inputs[1], text_inputs[2], text_inputs[3]

    if month_meta and day_meta and year_meta:
        print(
            "[生日字段] "
            f"month={month_meta.get('placeholder') or month_meta.get('name') or month_meta.get('ariaLabel')}, "
            f"day={day_meta.get('placeholder') or day_meta.get('name') or day_meta.get('ariaLabel')}, "
            f"year={year_meta.get('placeholder') or year_meta.get('name') or year_meta.get('ariaLabel')}"
        )
        month_input = page.locator("input").nth(int(month_meta["index"]))
        day_input = page.locator("input").nth(int(day_meta["index"]))
        year_input = page.locator("input").nth(int(year_meta["index"]))
        year_value = birth_year[-2:] if 0 < int(year_meta.get("maxLength") or 0) <= 2 else birth_year
        await type_slowly(page, month_input, birth_month)
        await type_slowly(page, day_input, birth_day)
        await type_slowly(page, year_input, year_value)
        return

    await page.keyboard.press("Tab")
    await page.keyboard.type(
        f"{birth_month}/{birth_day}/{birth_year}",
        delay=random.randint(50, 120),
    )


async def dump_frame_interactives(page, label: str) -> None:
    for index, frame in enumerate(page.frames):
        try:
            data = await frame.evaluate(
                """() => {
                    const textOf = (el) => ((el.innerText || el.textContent || el.value || "").trim()).replace(/\\s+/g, " ").slice(0, 120);
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                    };
                    const items = [...document.querySelectorAll('button, input, a, [role="button"], form')]
                        .filter((el) => el.tagName === "FORM" || visible(el))
                        .slice(0, 24)
                        .map((el) => ({
                            tag: el.tagName.toLowerCase(),
                            type: el.getAttribute("type") || "",
                            text: textOf(el),
                            name: el.getAttribute("name") || "",
                            placeholder: el.getAttribute("placeholder") || "",
                            role: el.getAttribute("role") || "",
                            href: el.getAttribute("href") || "",
                            action: el.getAttribute("action") || "",
                            disabled: !!el.disabled,
                        }));
                    return {
                        url: location.href,
                        title: document.title,
                        items,
                    };
                }"""
            )
            print(f"[{label}] frame#{index} url={data.get('url')} title={data.get('title')}")
            for item in data.get("items", []):
                print(
                    f"[{label}] frame#{index} "
                    f"{item.get('tag')} type={item.get('type')} role={item.get('role')} "
                    f"name={item.get('name')} placeholder={item.get('placeholder')} "
                    f"text={item.get('text')} href={item.get('href')} action={item.get('action')} "
                    f"disabled={item.get('disabled')}"
                )
        except Exception as exc:
            print(f"[{label}] frame#{index} inspect failed: {exc}")


async def try_keyboard_consent(page, label: str) -> bool:
    strategies = [
        ("Enter", None),
        ("Space", "Tab"),
        ("Space", "Shift+Tab"),
    ]

    for attempt, (submit_key, nav_key) in enumerate(strategies, start=1):
        try:
            if nav_key:
                await page.keyboard.press(nav_key)
                await page.wait_for_timeout(250)
            await page.keyboard.press(submit_key)
            await page.wait_for_timeout(4000)
            print(
                f"[{label}] keyboard attempt {attempt} nav={nav_key or 'none'} "
                f"submit={submit_key} url={page.url}"
            )
            if "consent" not in page.url:
                return True
        except Exception as exc:
            print(f"[{label}] keyboard attempt {attempt} failed: {exc}")
    return "consent" not in page.url


async def advance_consent_page(page, label: str) -> None:
    js = """() => {
        const textOf = (el) => ((el.innerText || el.textContent || el.value || "").trim()).replace(/\\s+/g, " ").slice(0, 120);
        const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
        };
        const keywords = /(continue|accept|allow|authorize|agree|consent|proceed|finish|sign in|continue to)/i;
        const clickables = [...document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"], a[href]')]
            .filter((el) => visible(el) && !el.disabled);
        const preferred = clickables.find((el) => keywords.test(textOf(el)))
            || clickables.find((el) => /submit|button/i.test(el.getAttribute("type") || ""))
            || clickables[0];
        if (preferred) {
            preferred.click();
            return {action: "click", text: textOf(preferred), tag: preferred.tagName.toLowerCase()};
        }
        const form = [...document.forms].find((el) => !el.querySelector('[disabled]'));
        if (form) {
            const submitter = form.querySelector('button:not([disabled]), input[type="submit"]:not([disabled]), input[type="button"]:not([disabled])');
            if (submitter) {
                submitter.click();
                return {action: "form-click", text: textOf(submitter), tag: submitter.tagName.toLowerCase()};
            }
            if (form.requestSubmit) {
                form.requestSubmit();
                return {action: "requestSubmit", text: "", tag: "form"};
            }
            form.submit();
            return {action: "submit", text: "", tag: "form"};
        }
        return {action: "none", text: "", tag: ""};
    }"""

    for index, frame in enumerate(page.frames):
        try:
            result = await asyncio.wait_for(frame.evaluate(js), timeout=5)
            print(f"[{label}] frame#{index} action={result.get('action')} tag={result.get('tag')} text={result.get('text')}")
            if result.get("action") != "none":
                return
        except Exception as exc:
            print(f"[{label}] frame#{index} submit failed: {exc}")

    await try_keyboard_consent(page, f"{label}_keyboard")


async def handle_about_you_page(page, name: str, birth_month: str, birth_day: str, birth_year: str) -> None:
    if "about-you" not in page.url and "about_you" not in page.url:
        return

    print("[处理] 检测到 about-you 页面，填写资料")
    try:
        name_input = await pick(
            page,
            [
                lambda p: p.locator('input[name="name"]'),
                lambda p: p.locator('input[placeholder*="Name"], input[placeholder*="名"]'),
                lambda p: p.locator('input[type="text"]'),
            ],
            5000,
        )
        if name_input:
            await type_slowly(page, name_input, name)
            await fill_birthday_fields(page, birth_month, birth_day, birth_year)
            await wait_after_input(page)

        finish_btn = await pick(
            page,
            [
                lambda p: p.get_by_role("button", name="完成帐户创建"),
                lambda p: p.get_by_role("button", name="Finish creating account"),
                lambda p: p.locator('button[type="submit"]'),
            ],
            8000,
        )
        if finish_btn:
            await safe_click(page, finish_btn)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            print(f"[处理] about-you 提交后 URL: {page.url}")
            try:
                body = await page.locator("body").inner_text(timeout=2000)
            except Exception:
                body = ""
            if body:
                print(f"[处理] about-you 提交后页面: {body[:300]}")
    except Exception as exc:
        print(f"[处理] about-you 页面处理异常: {exc}")


async def click_consent_action(page, label: str) -> bool:
    locator = await pick(
        page,
        [
            lambda p: p.get_by_role("button", name=re.compile(r"continue|accept|allow|authorize|agree", re.I)),
            lambda p: p.get_by_role("link", name=re.compile(r"continue|accept|allow|authorize|agree", re.I)),
            lambda p: p.locator(
                '[role="button"]:has-text("Continue"), [role="button"]:has-text("Accept"), '
                '[role="button"]:has-text("Allow"), [role="button"]:has-text("Authorize"), '
                '[role="button"]:has-text("Agree")'
            ),
            lambda p: p.locator(
                'a:has-text("Continue"), a:has-text("Accept"), a:has-text("Allow"), '
                'a:has-text("Authorize"), a:has-text("Agree")'
            ),
            lambda p: p.locator(
                'button:has-text("Continue"), button:has-text("Accept"), button:has-text("Allow"), '
                'button:has-text("Authorize"), button:has-text("Agree"), '
                'input[type="submit"], button[type="submit"]'
            ),
            lambda p: p.locator('form button, form [role="button"], form input[type="submit"], form a'),
        ],
        30000,
    )
    if not locator:
        print(f"[{label}] no consent locator found")
        return False

    try:
        text = (await locator.inner_text(timeout=2000)).strip()
    except Exception:
        try:
            text = (await locator.text_content(timeout=2000) or "").strip()
        except Exception:
            text = ""
    print(f"[{label}] clicking consent locator text={text}")
    await safe_click(page, locator)
    await page.wait_for_timeout(5000)
    print(f"[{label}] after click url={page.url}")
    return True


async def maybe_resend_email(page, label: str) -> None:
    resend_btn = await pick(
        page,
        [
            lambda p: p.get_by_role("button", name=re.compile(r"resend email|resend|重新发送", re.I)),
            lambda p: p.locator(
                'button:has-text("Resend email"), button:has-text("Resend"), '
                'button:has-text("重新发送"), button:has-text("重新获取")'
            ),
        ],
        3000,
    )
    if resend_btn:
        print(f"[{label}] click resend email")
        await safe_click(page, resend_btn)
        await page.wait_for_timeout(3000)


async def pick_password_input(page, timeout_ms: int = 10000):
    return await pick(
        page,
        [
            lambda p: p.locator(
                'input[type="password"], input[name="password"], '
                'input[autocomplete="new-password"], input[autocomplete="current-password"]'
            )
        ],
        timeout_ms,
    )


async def pick_code_input(page, timeout_ms: int = 10000):
    return await pick(
        page,
        [
            lambda p: p.get_by_role("textbox", name="Code"),
            lambda p: p.get_by_role("textbox", name="验证码"),
            lambda p: p.locator('input[name="code"], input[autocomplete="one-time-code"]'),
        ],
        timeout_ms,
    )


async def detect_post_login_stage(page, label: str, timeout_ms: int = 15000) -> str:
    end = time.time() + timeout_ms / 1000
    last_state = None

    while time.time() < end:
        url = page.url
        if "/auth/callback" in url:
            return "callback"
        if "consent" in url:
            return "consent"
        if "about-you" in url or "about_you" in url:
            return "about-you"
        if "email-verification" in url:
            return "otp"

        if await pick_code_input(page, 800):
            return "otp"
        if await pick_password_input(page, 800):
            return "password"

        title = await page_title_safe(page)
        body = await page_body_safe(page, limit=240)
        state = f"{url}|{title}|{body[:120]}"
        if state != last_state:
            print(f"[{label}] URL: {url}")
            print(f"[{label}] Title: {title or '<empty>'}")
            if body:
                print(f"[{label}] Body: {body}")
            last_state = state

        await page.wait_for_timeout(700)

    return "unknown"


async def is_email_verification_stage(page, timeout_ms: int = 1000) -> bool:
    if "email-verification" in page.url:
        return True
    return await pick_code_input(page, timeout_ms) is not None


async def complete_login_email_verification(
    page,
    worker_id: int,
    provider: str,
    email: str,
    mail_token: str,
    existing_mail_ids: set[str],
) -> bool:
    print_step(29, f"从 {provider_label(provider)} 收件箱读取登录验证码")
    login_otp_input = await pick_code_input(page, 15000)
    if not login_otp_input:
        await dump_page_debug(page, worker_id, "step29_login_otp_input_missing")
        return False

    first_wait = min(45, max(20, TEMPMAIL_TIMEOUT // 2))
    second_wait = max(20, TEMPMAIL_TIMEOUT - first_wait)
    login_otp = await get_verification_code_for_provider(
        provider,
        email,
        mail_token,
        timeout=first_wait,
        seen_ids=existing_mail_ids,
    )
    if not login_otp:
        await maybe_resend_email(page, "step29")
        login_otp = await get_verification_code_for_provider(
            provider,
            email,
            mail_token,
            timeout=second_wait,
            seen_ids=existing_mail_ids,
        )
    if not login_otp:
        await dump_page_debug(page, worker_id, "step29_login_otp_missing")
        return False

    # 第30步：输入登录验证码。
    print_step(30, f"输入登录验证码: {login_otp}")
    await type_slowly(page, login_otp_input, login_otp)

    # 第31步：输入登录验证码后等待。
    print_step(31, "输入登录验证码后等待")
    await wait_after_input(page)

    # 第32步：提交登录验证码。
    print_step(32, "提交登录验证码")
    login_verify_btn = await pick(
        page,
        [
            lambda p: p.get_by_role("button", name="Continue"),
            lambda p: p.get_by_role("button", name="继续"),
            lambda p: p.locator('button[type="submit"]'),
        ],
        5000,
    )
    if login_verify_btn:
        await safe_click(page, login_verify_btn)

    return True


def interesting_debug_url(url: str) -> bool:
    lowered = (url or "").lower()
    keywords = (
        "auth.openai.com",
        "chatgpt.com",
        "localhost:1455",
        "/oauth/",
        "oauth/authorize",
        "oauth/token",
        "consent",
        "email-verification",
        "backend-api/auth",
        "/api/auth",
        "sentinel.openai.com",
    )
    return any(keyword in lowered for keyword in keywords)


def attach_page_debug_listeners(page) -> None:
    def on_request(request) -> None:
        try:
            if request.resource_type in {"document", "xhr", "fetch"} and interesting_debug_url(request.url):
                print(f"[REQ] {request.method} {request.resource_type} {request.url}")
        except Exception:
            pass

    def on_response(response) -> None:
        try:
            request = response.request
            if request.resource_type in {"document", "xhr", "fetch"} and interesting_debug_url(response.url):
                print(f"[RESP] {response.status} {request.resource_type} {response.url}")
        except Exception:
            pass

    def on_frame_navigated(frame) -> None:
        try:
            if interesting_debug_url(frame.url):
                frame_name = "main" if frame == page.main_frame else (frame.name or "subframe")
                print(f"[NAV] {frame_name} {frame.url}")
        except Exception:
            pass

    def on_request_failed(request) -> None:
        try:
            if request.resource_type not in {"document", "xhr", "fetch"} or not interesting_debug_url(request.url):
                return
            failure = request.failure or {}
            error_text = failure.get("errorText") if isinstance(failure, dict) else str(failure)
            print(f"[REQ_FAILED] {request.method} {request.resource_type} {request.url} error={error_text}")
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("framenavigated", on_frame_navigated)
    page.on("requestfailed", on_request_failed)


def parse_html_attrs(raw_attrs: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in ATTR_RE.finditer(raw_attrs or ""):
        key = (match.group(1) or "").lower()
        value = match.group(2) or match.group(3) or match.group(4) or ""
        attrs[key] = html.unescape(value)
    return attrs


def strip_html_tags(raw_html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_html or "")).strip()


def extract_iframe_urls(raw_html: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for match in IFRAME_RE.finditer(raw_html or ""):
        src = html.unescape(match.group("src") or "").strip()
        if src:
            urls.append(urljoin(base_url, src))
    return urls


def extract_forms(raw_html: str, base_url: str) -> list[dict]:
    forms: list[dict] = []
    for form_match in FORM_RE.finditer(raw_html or ""):
        form_attrs = parse_html_attrs(form_match.group("attrs"))
        body = form_match.group("body") or ""
        action = urljoin(base_url, form_attrs.get("action") or base_url)
        method = (form_attrs.get("method") or "get").lower()
        inputs: list[dict[str, str]] = []
        buttons: list[dict[str, str]] = []

        for input_match in INPUT_RE.finditer(body):
            attrs = parse_html_attrs(input_match.group("attrs"))
            input_type = (attrs.get("type") or "text").lower()
            inputs.append(
                {
                    "name": attrs.get("name", ""),
                    "value": attrs.get("value", ""),
                    "type": input_type,
                    "checked": "checked" in attrs,
                }
            )

        for button_match in BUTTON_RE.finditer(body):
            attrs = parse_html_attrs(button_match.group("attrs"))
            button_type = (attrs.get("type") or "submit").lower()
            buttons.append(
                {
                    "name": attrs.get("name", ""),
                    "value": attrs.get("value", ""),
                    "type": button_type,
                    "text": strip_html_tags(button_match.group("body") or ""),
                }
            )

        forms.append(
            {
                "action": action,
                "method": method,
                "inputs": inputs,
                "buttons": buttons,
                "html": body,
            }
        )

    return forms


def choose_consent_form(forms: list[dict]) -> dict | None:
    keywords = re.compile(r"continue|accept|allow|authorize|agree|consent|proceed|finish|approve", re.I)
    best_form = None
    best_score = -1

    for form in forms:
        score = 0
        if form.get("method") == "post":
            score += 2
        for button in form.get("buttons", []):
            text = f"{button.get('text', '')} {button.get('value', '')}".strip()
            if keywords.search(text):
                score += 5
            elif button.get("type") == "submit":
                score += 1
        for field in form.get("inputs", []):
            if field.get("type") == "hidden":
                score += 1
        if score > best_score:
            best_score = score
            best_form = form

    return best_form


def build_form_payload(form: dict) -> dict[str, str]:
    payload: dict[str, str] = {}
    chosen_submit: dict | None = None
    keywords = re.compile(r"continue|accept|allow|authorize|agree|consent|proceed|finish|approve", re.I)

    for field in form.get("inputs", []):
        name = field.get("name", "")
        if not name:
            continue
        field_type = (field.get("type") or "text").lower()
        if field_type in {"checkbox", "radio"} and not field.get("checked"):
            continue
        if field_type in {"submit", "button", "image"}:
            text = f"{field.get('value', '')}".strip()
            if keywords.search(text) and chosen_submit is None:
                chosen_submit = field
            continue
        payload[name] = field.get("value", "")

    for button in form.get("buttons", []):
        button_type = (button.get("type") or "submit").lower()
        if button_type != "submit":
            continue
        text = f"{button.get('text', '')} {button.get('value', '')}".strip()
        if keywords.search(text):
            chosen_submit = button
            break
        if chosen_submit is None:
            chosen_submit = button

    if chosen_submit and chosen_submit.get("name"):
        payload[str(chosen_submit["name"])] = str(
            chosen_submit.get("value") or chosen_submit.get("text") or "true"
        )

    return payload


def save_text_debug_artifact(label: str, suffix: str, content: str) -> Path | None:
    try:
        base = Path(LOG_DIR)
        base.mkdir(parents=True, exist_ok=True)
        safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)
        path = base / f"{safe_label}.{suffix}"
        path.write_text(content, encoding="utf-8")
        print(f"[DEBUG_ARTIFACT:{label}] {path}")
        return path
    except Exception as exc:
        print(f"[DEBUG_ARTIFACT:{label}] failed: {exc}")
        return None


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def build_session_token_data(session_payload: dict) -> dict | None:
    access_token = str(session_payload.get("accessToken") or "").strip()
    if not access_token:
        return None

    claims = decode_jwt_payload(access_token)
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    profile_claims = claims.get("https://api.openai.com/profile") if isinstance(claims.get("https://api.openai.com/profile"), dict) else {}
    expires_at = parse_token_expiry_from_jwt(access_token) or parse_iso_datetime(session_payload.get("expires"))
    if expires_at is None:
        expires_in = 86400
    else:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires_in = max(300, int((expires_at - datetime.now(expires_at.tzinfo)).total_seconds()))

    account = session_payload.get("account") if isinstance(session_payload.get("account"), dict) else {}
    user = session_payload.get("user") if isinstance(session_payload.get("user"), dict) else {}
    return {
        "access_token": access_token,
        "account_id": auth_claims.get("chatgpt_account_id") or account.get("id") or "",
        "refresh_token": "",
        "id_token": "",
        "expires_in": expires_in,
        "session_expires": session_payload.get("expires") or "",
        "session_account_id": account.get("id") or "",
        "session_email": user.get("email") or "",
        "session_user_id": user.get("id") or "",
        "token_client_id": claims.get("client_id") or "",
        "token_email": profile_claims.get("email") or "",
        "token_issuer": claims.get("iss") or "",
        "token_scopes": claims.get("scp") or [],
    }


async def extract_chatgpt_session_token_data(page, label: str) -> dict | None:
    bootstrap_text = ""
    try:
        bootstrap_text = (await page.locator("script#client-bootstrap").text_content(timeout=5000)) or ""
    except Exception as exc:
        print(f"[{label}] client-bootstrap read failed: {exc}")

    if not bootstrap_text:
        try:
            html_text = await asyncio.wait_for(page.content(), timeout=8)
            match = re.search(
                r'<script[^>]+id="client-bootstrap"[^>]*>(?P<body>.*?)</script>',
                html_text,
                re.I | re.S,
            )
            if match:
                bootstrap_text = html.unescape(match.group("body"))
        except Exception as exc:
            print(f"[{label}] client-bootstrap fallback read failed: {exc}")

    if not bootstrap_text:
        return None

    try:
        payload = json.loads(bootstrap_text)
    except Exception as exc:
        print(f"[{label}] client-bootstrap parse failed: {exc}")
        return None

    session_payload = payload.get("session") if isinstance(payload, dict) else None
    if not isinstance(session_payload, dict):
        print(f"[{label}] session payload missing")
        return None

    token_data = build_session_token_data(session_payload)
    if token_data:
        print(
            f"[{label}] session extracted email={((session_payload.get('user') or {}).get('email') if isinstance(session_payload.get('user'), dict) else '')} "
            f"account_id={((session_payload.get('account') or {}).get('id') if isinstance(session_payload.get('account'), dict) else '')} "
            f"expires={session_payload.get('expires')}"
        )
    return token_data


async def verify_session_access_token(
    access_token: str,
    *,
    expected_email: str = "",
    expected_account_id: str = "",
) -> bool:
    payload = decode_jwt_payload(access_token)
    if not payload:
        print("[session_token] verify failed: jwt payload empty")
        return False

    issues: list[str] = []
    issuer = str(payload.get("iss") or "").strip()
    if issuer != "https://auth.openai.com":
        issues.append(f"unexpected issuer={issuer!r}")

    audience = payload.get("aud")
    if isinstance(audience, str):
        audiences = [audience]
    elif isinstance(audience, list):
        audiences = [str(item).strip() for item in audience if str(item).strip()]
    else:
        audiences = []
    if "https://api.openai.com/v1" not in audiences:
        issues.append(f"unexpected aud={audiences!r}")

    scopes_raw = payload.get("scp")
    if isinstance(scopes_raw, str):
        scopes = [item.strip() for item in scopes_raw.split() if item.strip()]
    elif isinstance(scopes_raw, list):
        scopes = [str(item).strip() for item in scopes_raw if str(item).strip()]
    else:
        scopes = []
    missing_scopes = sorted({"openid", "email", "profile"} - set(scopes))
    if missing_scopes:
        issues.append(f"missing scopes={missing_scopes!r}")

    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and float(exp) <= time.time() + 30:
        issues.append("token expired")

    auth_payload = payload.get("https://api.openai.com/auth") if isinstance(payload.get("https://api.openai.com/auth"), dict) else {}
    profile_payload = payload.get("https://api.openai.com/profile") if isinstance(payload.get("https://api.openai.com/profile"), dict) else {}
    account_id = str(auth_payload.get("chatgpt_account_id") or "").strip()
    email = str(profile_payload.get("email") or "").strip()

    if not account_id:
        issues.append("missing chatgpt_account_id")
    if not email:
        issues.append("missing profile.email")
    if expected_email and email.lower() != expected_email.strip().lower():
        issues.append(f"email mismatch={email!r}")
    if expected_account_id and account_id != expected_account_id.strip():
        issues.append(f"account_id mismatch={account_id!r}")

    client_id = str(payload.get("client_id") or "").strip()
    print(
        f"[session_token] verify client_id={client_id} "
        f"email={email} account_id={account_id} scopes={scopes}"
    )
    if issues:
        print(f"[session_token] verify issues: {'; '.join(issues)}")
        return False
    return True


async def request_with_local_callback_support(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    max_redirects: int = 10,
) -> tuple[str, httpx.Response | None, bool]:
    current_url = url
    current_method = method.upper()
    current_data = data

    for _ in range(max_redirects):
        response = await client.request(
            current_method,
            current_url,
            data=current_data if current_method != "GET" else None,
            headers=headers,
            follow_redirects=False,
        )
        if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
            next_url = urljoin(str(response.url), response.headers["location"])
            print(f"[consent_http] redirect {response.status_code} -> {next_url}")
            if next_url.startswith(REDIRECT_URI) or next_url.startswith(f"http://127.0.0.1:{CALLBACK_PORT}/"):
                async with httpx.AsyncClient(timeout=10, mounts={"http://": None, "https://": None}) as local_client:
                    local_response = await local_client.get(next_url, follow_redirects=False)
                return next_url, local_response, True
            current_url = next_url
            if response.status_code in {301, 302, 303}:
                current_method = "GET"
                current_data = None
            continue
        return str(response.url), response, False

    return current_url, None, False


async def submit_consent_via_http(context, page, consent_url: str, label: str) -> bool:
    cookies = httpx.Cookies()
    for cookie in await context.cookies():
        try:
            cookies.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain=(cookie.get("domain") or "").lstrip(".") or None,
                path=cookie.get("path") or "/",
            )
        except Exception:
            pass

    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept-Language": f"{BROWSER_LOCALE},{BROWSER_LOCALE.split('-')[0]};q=0.9",
    }
    client_kwargs = {"timeout": 20, "headers": headers, "cookies": cookies, "follow_redirects": False}
    if API_PROXY:
        client_kwargs["proxy"] = API_PROXY

    documents: list[tuple[str, str]] = []
    async with httpx.AsyncClient(**client_kwargs) as client:
        final_url, response, callback_hit = await request_with_local_callback_support(
            client,
            "GET",
            consent_url,
            headers={
                **headers,
                "Referer": consent_url,
            },
        )
        if callback_hit:
            print(f"[{label}] consent GET hit callback directly: {final_url}")
            return True
        if response is None:
            return False

        save_text_debug_artifact(f"{label}_root", "html", response.text)
        documents.append((final_url, response.text))
        iframe_candidates: list[str] = []
        iframe_candidates.extend(extract_iframe_urls(response.text, final_url))
        iframe_candidates.extend(
            frame.url
            for frame in page.frames
            if getattr(frame, "url", "") and getattr(frame, "url", "") != "about:blank"
        )

        seen_urls: set[str] = set()
        for iframe_url in iframe_candidates[:8]:
            if not iframe_url or iframe_url in seen_urls or iframe_url == final_url:
                continue
            seen_urls.add(iframe_url)
            try:
                iframe_response = await client.get(
                    iframe_url,
                    headers={
                        **headers,
                        "Referer": final_url,
                    },
                    follow_redirects=False,
                )
                documents.append((str(iframe_response.url), iframe_response.text))
                print(f"[{label}] fetched iframe: {iframe_response.url}")
                save_text_debug_artifact(
                    f"{label}_iframe_{len(documents) - 1}",
                    "html",
                    iframe_response.text,
                )
            except Exception as exc:
                print(f"[{label}] fetch iframe failed: {iframe_url} {exc}")

        all_forms: list[dict] = []
        for doc_url, doc_html in documents:
            forms = extract_forms(doc_html, doc_url)
            print(f"[{label}] forms from {doc_url}: {len(forms)}")
            all_forms.extend(forms)

        consent_form = choose_consent_form(all_forms)
        if not consent_form:
            print(f"[{label}] no consent form found")
            return False

        payload = build_form_payload(consent_form)
        print(
            f"[{label}] submit form method={consent_form.get('method')} action={consent_form.get('action')} "
            f"fields={sorted(payload.keys())}"
        )
        submit_headers = {
            **headers,
            "Referer": consent_form.get("action") or consent_url,
            "Origin": f"{urlparse(consent_form.get('action') or consent_url).scheme}://{urlparse(consent_form.get('action') or consent_url).netloc}",
        }
        final_url, _, callback_hit = await request_with_local_callback_support(
            client,
            consent_form.get("method", "post"),
            consent_form.get("action") or consent_url,
            data=payload,
            headers=submit_headers,
        )
        print(f"[{label}] submit result url={final_url} callback_hit={callback_hit}")
        return callback_hit


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for_cdp_ws_endpoint(port: int, timeout: int = 60) -> str:
    deadline = time.time() + timeout
    last_error = None
    async with httpx.AsyncClient(timeout=3, mounts={"http://": None, "https://": None}) as client:
        while time.time() < deadline:
            for endpoint in (
                f"http://127.0.0.1:{port}/json/version",
                f"http://127.0.0.1:{port}/json",
            ):
                try:
                    resp = await client.get(endpoint)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    if isinstance(data, dict):
                        ws_url = data.get("webSocketDebuggerUrl")
                        if ws_url:
                            return ws_url
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict) and item.get("webSocketDebuggerUrl"):
                                return item["webSocketDebuggerUrl"]
                except Exception as exc:
                    last_error = exc
            await asyncio.sleep(0.5)
    if last_error:
        raise RuntimeError(f"CDP endpoint did not become ready on port {port}: {last_error}")
    raise RuntimeError(f"CDP endpoint did not become ready on port {port}")


def stop_process_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), 15)
                time.sleep(0.5)
            except Exception:
                pass
            if process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), 9)
                except Exception:
                    process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def cleanup_profile_dir(path: Path | None) -> None:
    if not path:
        return
    try:
        if path.exists():
            shutil.rmtree(path)
            log.debug("Removed profile directory %s", path)
    except Exception as exc:
        log.debug("Failed to remove profile directory %s: %s", path, exc)


async def launch_context(playwright):
    worker_id = random.randint(WORKER_ID_MIN, WORKER_ID_MAX)
    profile_dir = Path(VB_PROFILE_ROOT) / str(worker_id)
    profile_dir.mkdir(parents=True, exist_ok=True)
    process = None
    browser = None
    context = None
    playwright_proxy = build_playwright_proxy(BROWSER_PROXY)

    try:
        if VB_EXE:
            cdp_port = find_free_port()
            command = [
                resolve_virtualbrowser_exe(),
                f"--user-data-dir={profile_dir}",
                f"--remote-debugging-port={cdp_port}",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-sandbox",
                "--disable-web-security",
                f"--lang={BROWSER_LOCALE}",
                f"--window-size={BROWSER_VIEWPORT['width']},{BROWSER_VIEWPORT['height']}",
            ]

            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            popen_kwargs = {"creationflags": creation_flags} if os.name == "nt" else {"start_new_session": True}
            chrome_env = {**os.environ, "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}

            if BROWSER_PROXY:
                proxy_server = extract_proxy_server(BROWSER_PROXY)
                if proxy_server:
                    command.append(f"--proxy-server={proxy_server}")
                    command.append("--proxy-bypass-list=localhost,127.0.0.1")
                    # Extract credentials for environment variables
                    parsed = urlparse(BROWSER_PROXY)
                    if parsed.username and parsed.password:
                        chrome_env["http_proxy"] = BROWSER_PROXY
                        chrome_env["https_proxy"] = BROWSER_PROXY
                        chrome_env["HTTP_PROXY"] = BROWSER_PROXY
                        chrome_env["HTTPS_PROXY"] = BROWSER_PROXY
            if HEADLESS:
                command.append("--headless=new")

            process = subprocess.Popen(command, env=chrome_env, **popen_kwargs)
            ws_endpoint = await wait_for_cdp_ws_endpoint(cdp_port)
            await asyncio.sleep(3)  # 等待 Chrome 完全就绪
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint)
            if not browser.contexts:
                raise RuntimeError("VirtualBrowser did not expose a default browser context")
            context = browser.contexts[0]
        else:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-pings",
                "--no-sandbox",
                "--host-resolver-rules=MAP localhost 127.0.0.1,MAP *.localhost 127.0.0.1",
                "--proxy-bypass-list=localhost;127.0.0.1;::1",
            ]
            if BROWSER_PROXY and not playwright_proxy:
                proxy_server = extract_proxy_server(BROWSER_PROXY)
                if proxy_server:
                    launch_args.append(f"--proxy-server={proxy_server}")

            launch_kwargs = {
                "user_data_dir": str(profile_dir),
                "headless": HEADLESS,
                "args": launch_args,
                "ignore_default_args": ["--enable-automation"],
                "locale": BROWSER_LOCALE,
                "timezone_id": BROWSER_TIMEZONE,
                "viewport": BROWSER_VIEWPORT,
                "screen": BROWSER_VIEWPORT,
                "color_scheme": "light",
                "user_agent": BROWSER_USER_AGENT,
                "extra_http_headers": {
                    "Accept-Language": f"{BROWSER_LOCALE},{BROWSER_LOCALE.split('-')[0]};q=0.9",
                },
            }
            if playwright_proxy:
                launch_kwargs["proxy"] = playwright_proxy

            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        stop_process_tree(process)
        raise

    try:
        await context.clear_cookies()
    except Exception:
        pass

    await context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{},app:{}};"
        f"Object.defineProperty(navigator,'languages',{{get:()=>['{BROWSER_LOCALE}','{BROWSER_LOCALE.split('-')[0]}']}});"
        "Object.defineProperty(navigator,'platform',{get:()=> 'Linux x86_64'});"
        "Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});"
        "Object.defineProperty(navigator,'deviceMemory',{get:()=>8});"
    )
    page = context.pages[0] if context.pages else await context.new_page()
    attach_page_debug_listeners(page)
    try:
        await Stealth().apply_stealth_async(page)
    except Exception:
        pass
    page.set_default_timeout(60000)
    page.set_default_navigation_timeout(90000)

    print(
        f"Using {'VirtualBrowser' if browser else 'Playwright Chromium'} worker-id: {worker_id}"
    )
    return context, page, worker_id, browser, process, profile_dir


def print_step(step_no: int, message: str) -> None:
    print(f"[第{step_no}步] {message}")


async def dump_page_debug(page, worker_id: int, label: str) -> None:
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)
    screenshot_path = Path(LOG_DIR) / f"{safe_label}_{worker_id}.png"

    try:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[DEBUG:{label}] screenshot: {screenshot_path}")
    except Exception as exc:
        print(f"[DEBUG:{label}] screenshot failed: {exc}")

    try:
        title = await page.title()
    except Exception as exc:
        title = f"<title unavailable: {exc}>"

    try:
        body_text = await page.evaluate("document.body ? document.body.innerText : ''")
    except Exception as exc:
        body_text = f"<body unavailable: {exc}>"

    print(f"[DEBUG:{label}] URL: {page.url}")
    print(f"[DEBUG:{label}] Title: {title}")
    print(f"[DEBUG:{label}] Body: {body_text[:1200]}")


async def save_page_snapshot(page, worker_id: int, label: str) -> None:
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)
    base = Path(LOG_DIR)
    base.mkdir(parents=True, exist_ok=True)
    html_path = base / f"{safe_label}_{worker_id}.html"
    json_path = base / f"{safe_label}_{worker_id}.json"
    screenshot_path = base / f"{safe_label}_{worker_id}.png"

    try:
        html = await asyncio.wait_for(page.content(), timeout=5)
        html_path.write_text(html, encoding="utf-8")
        print(f"[SNAPSHOT:{label}] html: {html_path}")
    except Exception as exc:
        print(f"[SNAPSHOT:{label}] html failed: {exc}")

    try:
        data = []
        for index, frame in enumerate(page.frames):
            try:
                frame_data = await frame.evaluate(
                    """() => {
                        const textOf = (el) => ((el.innerText || el.textContent || el.value || "").trim()).replace(/\\s+/g, " ").slice(0, 200);
                        const visible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style && style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
                        };
                        return {
                            url: location.href,
                            title: document.title,
                            items: [...document.querySelectorAll('button, input, a, [role="button"], form')]
                                .filter((el) => el.tagName === "FORM" || visible(el))
                                .slice(0, 50)
                                .map((el) => ({
                                    tag: el.tagName.toLowerCase(),
                                    type: el.getAttribute("type") || "",
                                    text: textOf(el),
                                    name: el.getAttribute("name") || "",
                                    placeholder: el.getAttribute("placeholder") || "",
                                    role: el.getAttribute("role") || "",
                                    href: el.getAttribute("href") || "",
                                    action: el.getAttribute("action") || "",
                                    disabled: !!el.disabled,
                                })),
                        };
                    }"""
                )
                frame_data["frame_index"] = index
                data.append(frame_data)
            except Exception as exc:
                data.append({"frame_index": index, "error": str(exc), "url": frame.url})
        json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[SNAPSHOT:{label}] json: {json_path}")
    except Exception as exc:
        print(f"[SNAPSHOT:{label}] json failed: {exc}")

    try:
        await asyncio.wait_for(page.screenshot(path=str(screenshot_path), full_page=True), timeout=5)
        print(f"[SNAPSHOT:{label}] screenshot: {screenshot_path}")
    except Exception as exc:
        print(f"[SNAPSHOT:{label}] screenshot failed: {exc}")


async def log_page_body(page, label: str, limit: int = 400) -> str:
    try:
        text = await asyncio.wait_for(
            page.evaluate("document.body ? document.body.innerText : ''"),
            timeout=8,
        )
    except Exception as exc:
        print(f"[{label}] body read failed: {exc}")
        return ""

    print(f"[{label}] 页面: {text[:limit]}")
    return text


async def save_page_snapshot_safe(page, worker_id: int, label: str, timeout: int = 20) -> None:
    try:
        await asyncio.wait_for(save_page_snapshot(page, worker_id, label), timeout=timeout)
    except Exception as exc:
        print(f"[SNAPSHOT:{label}] wrapper failed: {exc}")


async def dismiss_chatgpt_onboarding(page, label: str) -> None:
    for attempt in range(1, 5):
        if "chatgpt.com" not in page.url:
            return

        skip_btn = await pick(
            page,
            [
                lambda p: p.get_by_role("button", name="Skip", exact=True),
                lambda p: p.get_by_role("button", name="跳过", exact=True),
                lambda p: p.locator('button:has-text("Skip"), button:has-text("跳过")'),
            ],
            3000,
        )
        if skip_btn:
            print(f"[{label}] click onboarding Skip attempt={attempt}")
            await safe_click(page, skip_btn)
            await page.wait_for_timeout(3000)
            continue

        next_btn = await pick(
            page,
            [
                lambda p: p.get_by_role("button", name="Next", exact=True),
                lambda p: p.locator('button:has-text("Next")'),
            ],
            2000,
        )
        choice_btn = await pick(
            page,
            [
                lambda p: p.get_by_role("button", name="Work", exact=True),
                lambda p: p.get_by_role("button", name="Personal tasks", exact=True),
                lambda p: p.get_by_role("button", name="Other", exact=True),
                lambda p: p.locator(
                    'button:has-text("Work"), button:has-text("Personal tasks"), '
                    'button:has-text("Other"), button:has-text("School")'
                ),
            ],
            2000,
        )
        if choice_btn and next_btn:
            print(f"[{label}] choose onboarding option then Next attempt={attempt}")
            await safe_click(page, choice_btn)
            await page.wait_for_timeout(1000)
            await safe_click(page, next_btn)
            await page.wait_for_timeout(3000)
            continue

        print(f"[{label}] no onboarding controls found attempt={attempt} url={page.url}")
        return


async def register_one(playwright) -> bool:
    # 第1步：生成 OAuth 参数和账号密码。
    print_step(1, "生成 OAuth 参数和账号密码")
    verifier, challenge = generate_pkce_codes()
    state = secrets.token_urlsafe(32)
    auth_url = build_auth_url(challenge, state)
    password = build_password()
    print(f"OpenAI 授权页面: {auth_url}")

    # 第2步：申请临时邮箱。
    print_step(2, "申请临时邮箱")
    email_info = await allocate_temporary_email()
    if not email_info:
        return False
    provider = email_info["provider"]
    email = email_info.get("email", "")
    mail_token = email_info.get("token", "")
    name = "".join(random.choices(string.ascii_letters, k=random.randint(5, 8))).capitalize()
    print(f"{provider_label(provider)} 邮箱: {email}")

    # 第3步：准备 OAuth 回调等待。
    print_step(3, "准备 OAuth 回调等待")

    # 第4步：启动随机 VirtualBrowser worker 并附着 Playwright。
    print_step(4, "启动随机 VirtualBrowser worker 并附着 Playwright")
    browser = None
    process = None
    profile_dir = None
    context, page, worker_id, browser, process, profile_dir = await launch_context(playwright)
    try:
        print(f"Email: {email}  Name: {name}  Password: {password}  Worker: {worker_id}")
        session_token_data: dict | None = None

        # 第5步：打开注册页面。
        print_step(5, "打开注册页面")
        login_url = "https://chatgpt.com/auth/login_with"
        await page.goto(login_url, wait_until="domcontentloaded")
        # Wait for React to hydrate and render the form
        await page.wait_for_timeout(5000)
        await wait_for_login_ready(page)

        # 第6步：进入注册流程。
        print_step(6, "进入注册流程")
        sign_up = await pick(
            page,
            [
                lambda p: p.get_by_role("link", name="Sign up"),
                lambda p: p.get_by_role("link", name="注册"),
                lambda p: p.get_by_role("button", name="Create account"),
                lambda p: p.get_by_role("button", name="Sign up"),
                lambda p: p.locator('a:has-text("Sign up"), a:has-text("注册")'),
                lambda p: p.locator('text=/Sign up/i'),
                lambda p: p.locator('text=/Create account/i'),
                lambda p: p.locator('text=/Register/i'),
                lambda p: p.locator('text=/注册|新帐户/'),
                lambda p: p.locator('text=/免费注册/'),
                lambda p: p.locator('text=/join.*chatgpt/i'),
            ],
            15000,
        )
        if sign_up:
            await safe_click(page, sign_up)
            await wait_for_login_ready(page, timeout_ms=45000)
        else:
            print("[第6步] 未找到注册入口，跳过继续流程")

        # 第7步：输入临时邮箱地址。
        print_step(7, "输入临时邮箱地址")
        email_input = await pick(
            page,
            [
                lambda p: p.get_by_role("textbox", name="Email address"),
                lambda p: p.get_by_role("textbox", name="电子邮件地址"),
                lambda p: p.locator('input[type="email"], input[name="email"]'),
            ],
            60000,
        )
        if not email_input:
            await dump_page_debug(page, worker_id, "step7_email_input_missing")
            return False
        await type_slowly(page, email_input, email)

        # 第8步：输入邮箱后等待。
        print_step(8, "输入邮箱后等待")
        await wait_after_input(page)

        # 第9步：提交邮箱步骤。
        print_step(9, "提交邮箱步骤")
        continue_btn = await pick(
            page,
            [
                lambda p: p.get_by_role("button", name="Continue", exact=True),
                lambda p: p.get_by_role("button", name="继续", exact=True),
                lambda p: p.locator('button[type="submit"]'),
            ],
            10000,
        )
        if not continue_btn:
            await dump_page_debug(page, worker_id, "step9_continue_missing")
            return False
        await safe_click(page, continue_btn)

        # 第10步：等待密码页。
        print_step(10, "等待密码页出现")
        password_inputs = page.locator(
            'input[type="password"], input[name="password"], '
            'input[autocomplete="new-password"], input[autocomplete="current-password"]'
        )
        password_count = 0
        for _ in range(20):
            try:
                password_count = await password_inputs.count()
            except Exception:
                password_count = 0
            if password_count > 0:
                break
            await page.wait_for_timeout(500)

        # 第11步：输入密码。
        print_step(11, "输入密码")
        if password_count > 0:
            filled = 0
            for idx in range(password_count):
                field = password_inputs.nth(idx)
                try:
                    if await field.is_visible():
                        await type_slowly(page, field, password)
                        filled += 1
                        await page.wait_for_timeout(random.randint(120, 300))
                except Exception:
                    pass
            if filled == 0:
                return False

            # 第12步：输入密码后等待。
            print_step(12, "输入密码后等待")
            await wait_after_input(page)

            # 第13步：提交密码页面。
            print_step(13, "提交密码页面")
            password_continue_btn = await pick(
                page,
                [
                    lambda p: p.get_by_role("button", name="Continue", exact=True),
                    lambda p: p.get_by_role("button", name="继续", exact=True),
                    lambda p: p.locator('button[type="submit"]'),
                ],
                8000,
            )
            if not password_continue_btn:
                return False
            await safe_click(page, password_continue_btn)

        # 第14步：等待验证码页面。
        print_step(14, "等待验证码页面")
        try:
            await page.wait_for_url("**/email-verification*", timeout=5000)
        except Exception:
            pass

        # 第15步：从临时邮箱收件箱读取验证码。
        print_step(15, f"从 {provider_label(provider)} 收件箱读取验证码")
        await page.screenshot(path=f"/tmp/otp_wait_{worker_id}.png")
        print(f"截图: /tmp/otp_wait_{worker_id}.png")
        print(f"当前URL: {page.url}")
        print(f"[DEBUG] 邮箱: {email} | Token: {mail_token} | Provider: {provider}")
        otp = await get_verification_code_for_provider(provider, email, mail_token, timeout=TEMPMAIL_TIMEOUT)
        if not otp:
            return False

        # 第16步：输入验证码。
        print_step(16, f"输入验证码: {otp}")
        otp_input = await pick(
            page,
            [
                lambda p: p.get_by_role("textbox", name="Code"),
                lambda p: p.get_by_role("textbox", name="验证码"),
                lambda p: p.locator('input[name="code"], input[autocomplete="one-time-code"]'),
            ],
            10000,
        )
        if not otp_input:
            return False
        await type_slowly(page, otp_input, otp)

        # 第17步：输入验证码后等待。
        print_step(17, "输入验证码后等待")
        await wait_after_input(page)

        # 第18步：提交验证码。
        print_step(18, "提交验证码")
        verify_btn = await pick(
            page,
            [
                lambda p: p.get_by_role("button", name="Continue"),
                lambda p: p.get_by_role("button", name="继续"),
                lambda p: p.locator('button[type="submit"]'),
            ],
            5000,
        )
        if verify_btn:
            await safe_click(page, verify_btn)

        # 第19步：填写基础资料。
        print_step(19, "填写基础资料")
        name_input = await pick(
            page,
            [
                lambda p: p.locator('input[name="name"]'),
                lambda p: p.locator('input[placeholder*="Name"], input[placeholder*="名"]'),
                lambda p: p.locator('input[type="text"]'),
            ],
            15000,
        )
        if not name_input:
            return False
        await type_slowly(page, name_input, name)
        birth_month = f"{random.randint(1, 12):02d}"
        birth_day = f"{random.randint(1, 28):02d}"
        birth_year = str(random.randint(1985, 1995))
        await fill_birthday_fields(page, birth_month, birth_day, birth_year)

        # 第20步：输入基础资料后等待。
        print_step(20, "输入基础资料后等待")
        await wait_after_input(page)

        # 第21步：提交基础资料页面。
        print_step(21, "提交基础资料页面")
        about_btn = await pick(
            page,
            [
                lambda p: p.get_by_role("button", name="Continue"),
                lambda p: p.get_by_role("button", name="继续"),
                lambda p: p.locator('button[type="submit"]'),
            ],
            5000,
        )
        if about_btn:
            await safe_click(page, about_btn)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass

        await wait_after_input(page)

        # 第22步：重新打开 auth_url。
        print_step(22, "重新打开 auth_url")
        existing_mail_ids = await get_message_ids_for_provider(provider, mail_token)
        await page.goto(auth_url, wait_until="domcontentloaded")

        # 第23步：输入登录邮箱。
        print_step(23, "输入登录邮箱")
        login_otp_completed = False
        login_email_input = await pick(
            page,
            [
                lambda p: p.get_by_role("textbox", name="Email address"),
                lambda p: p.get_by_role("textbox", name="电子邮件地址"),
                lambda p: p.locator('input[type="email"], input[name="email"]'),
            ],
            10000,
        )
        if login_email_input:
            await type_slowly(page, login_email_input, email)

            # 第24步：输入登录邮箱后等待。
            print_step(24, "输入登录邮箱后等待")
            await wait_after_input(page)

            # 第25步：提交登录邮箱。
            print_step(25, "提交登录邮箱")
            login_continue_btn = await pick(
                page,
                [
                    lambda p: p.get_by_role("button", name="Continue", exact=True),
                    lambda p: p.get_by_role("button", name="继续", exact=True),
                    lambda p: p.locator('button[type="submit"]'),
                ],
                10000,
            )
            if not login_continue_btn:
                return False
            await safe_click(page, login_continue_btn)

            next_stage = await detect_post_login_stage(page, "step26")
            print(f"[第26步] 登录邮箱提交后阶段: {next_stage} URL: {page.url}")

            if next_stage == "password":
                # 第26步：输入登录密码。
                print_step(26, "输入登录密码")
                login_password_input = await pick_password_input(page, 10000)
                if not login_password_input:
                    await dump_page_debug(page, worker_id, "step26_password_missing")
                    return False
                await type_slowly(page, login_password_input, password)

                # 第27步：输入登录密码后等待。
                print_step(27, "输入登录密码后等待")
                await wait_after_input(page)

                # 第28步：提交登录密码。
                print_step(28, "提交登录密码")
                login_password_continue_btn = await pick(
                    page,
                    [
                        lambda p: p.get_by_role("button", name="Continue", exact=True),
                        lambda p: p.get_by_role("button", name="继续", exact=True),
                        lambda p: p.locator('button[type="submit"]'),
                    ],
                    8000,
                )
                if not login_password_continue_btn:
                    return False
                await safe_click(page, login_password_continue_btn)
                next_stage = await detect_post_login_stage(page, "step28")
                print(f"[第28步] 登录密码提交后阶段: {next_stage} URL: {page.url}")
            elif next_stage == "otp":
                print("[第26步] 已直接进入邮箱验证码页，跳过密码步骤")
            elif next_stage in {"consent", "callback", "about-you"}:
                print(f"[第26步] 当前阶段无需二次密码验证: {next_stage}")
            else:
                await dump_page_debug(page, worker_id, "step26_unknown_stage")

            if next_stage == "otp":
                login_otp_completed = await complete_login_email_verification(
                    page,
                    worker_id,
                    provider,
                    email,
                    mail_token,
                    existing_mail_ids,
                )
                if not login_otp_completed:
                    return False

        if not login_otp_completed and await is_email_verification_stage(page, 3000):
            print("[第29步] 检测到延迟出现的登录验证码页，补跑验证码流程")
            login_otp_completed = await complete_login_email_verification(
                page,
                worker_id,
                provider,
                email,
                mail_token,
                existing_mail_ids,
            )
            if not login_otp_completed:
                return False

        await handle_about_you_page(page, name, birth_month, birth_day, birth_year)

        # 第33步：处理授权确认页面。
        print_step(33, "处理授权确认页面")
        await wait_after_input(page)
        try:
            buttons = await page.locator("button").evaluate_all(
                """els => els.map((el, index) => ({
                    index,
                    text: (el.innerText || el.textContent || "").trim(),
                    type: el.getAttribute("type") || "",
                    disabled: !!el.disabled
                }))"""
            )
            print(
                "[授权页按钮] "
                + "; ".join(
                    f"#{item.get('index')} text={item.get('text')} type={item.get('type')} disabled={item.get('disabled')}"
                    for item in buttons
                )
            )
        except Exception:
            pass

        consent_btn = await pick(
            page,
            [
                lambda p: p.locator(
                    'button:has-text("Continue"), button:has-text("继续"), '
                    'button:has-text("Accept"), button:has-text("同意"), '
                    'button:has-text("Allow"), button:has-text("Authorize"), '
                    'button:has-text("Continue to"), button[type="submit"]'
                ),
                lambda p: p.locator('form button'),
            ],
            15000,
        )
        if consent_btn:
            try:
                btn_text = (await consent_btn.inner_text(timeout=2000)).strip()
            except Exception:
                try:
                    btn_text = (await consent_btn.text_content(timeout=2000) or "").strip()
                except Exception:
                    btn_text = ""
            print(f"[第33步] 点击授权页按钮: {btn_text}")
            await safe_click(page, consent_btn)
            await page.wait_for_timeout(3000)
            print(f"[第33步] 点击后 URL: {page.url}")
        elif "consent" in page.url:
            try:
                await page.keyboard.press("Tab")
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(3000)
                print(f"[第33步] 键盘后 URL: {page.url}")
            except Exception:
                pass

        # 第34步：等待 OAuth 回调。
        print_step(34, "等待本地 OAuth 回调")
        try:
            await page.screenshot(path=f"/tmp/step34_{worker_id}.png", timeout=10000)
        except Exception as exc:
            print(f"[step34] screenshot failed: {exc}")
        print(f"第34步 URL: {page.url}")

        if "about-you" in page.url or "about_you" in page.url:
            await handle_about_you_page(page, name, birth_month, birth_day, birth_year)
            print(f"[step34 about-you 后 URL] {page.url}")
            await log_page_body(page, "step34 about-you 后", 400)
            await save_page_snapshot_safe(page, worker_id, "step34_after_about_you")
        if "consent" in page.url:
            http_submitted = False
            try:
                http_submitted = await submit_consent_via_http(context, page, page.url, "step34_http")
            except Exception as exc:
                print(f"[step34] consent http failed: {exc}")
            if "consent" in page.url and not http_submitted:
                await try_keyboard_consent(page, "step34_pre")
            clicked = False
            if "consent" in page.url and not http_submitted:
                try:
                    clicked = await asyncio.wait_for(click_consent_action(page, "step34"), timeout=10)
                except Exception as exc:
                    print(f"[step34] click consent failed: {exc}")
            if "consent" in page.url and not clicked and not http_submitted:
                await advance_consent_page(page, "step34")
            elif "consent" in page.url and not http_submitted:
                await advance_consent_page(page, "step34_fallback")
            print(f"[step34 提交后 URL] {page.url}")
            await log_page_body(page, "step34 提交后", 400)
            await save_page_snapshot_safe(page, worker_id, "step34_after_consent")
        else:
            await log_page_body(page, "step34", 400)
            await save_page_snapshot_safe(page, worker_id, "step34")

        if "chatgpt.com" in page.url and "/auth/" not in page.url and "/api/" not in page.url:
            print("[step34] 检测到已进入 ChatGPT 站点，尝试跳过引导并重试 auth_url")
            await dismiss_chatgpt_onboarding(page, "step34_onboarding")
            print(f"[step34 onboarding 后 URL] {page.url}")
            await log_page_body(page, "step34 onboarding 后", 400)
            await save_page_snapshot_safe(page, worker_id, "step34_after_onboarding")
            session_token_data = await extract_chatgpt_session_token_data(page, "step34_session")

            try:
                await page.goto(auth_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(4000)
                print(f"[step34 retry auth_url 后 URL] {page.url}")
                await log_page_body(page, "step34 retry auth_url", 400)
                await save_page_snapshot_safe(page, worker_id, "step34_retry_auth")
            except Exception as exc:
                print(f"[step34] retry auth_url failed: {exc}")

            if "consent" in page.url:
                print("[step34] retry auth_url 后再次遇到 consent，补做一次提交")
                try:
                    await submit_consent_via_http(context, page, page.url, "step34_retry_http")
                except Exception as exc:
                    print(f"[step34] retry consent http failed: {exc}")
                if "consent" in page.url:
                    await try_keyboard_consent(page, "step34_retry_keyboard")
                    if "consent" in page.url:
                        await advance_consent_page(page, "step34_retry")
        result = await wait_for_oauth_result(state, timeout=45 if session_token_data else 120)
        if not result or result.get("error") or not result.get("code") or result.get("state") != state:
            if session_token_data:
                print("[step34] OAuth 回调缺失，尝试使用 ChatGPT 会话 access token 作为回退")
                if await verify_session_access_token(
                    session_token_data["access_token"],
                    expected_email=email,
                    expected_account_id=str(session_token_data.get("account_id") or session_token_data.get("session_account_id") or ""),
                ):
                    session_token_data["account_password"] = password
                    session_token_data["mail_token"] = mail_token
                    path = save_tokens(email, session_token_data)
                    print(f"Session fallback success, saved token: {path}")
                    log.info(f"session_fallback: email={email}, worker_id={worker_id}, path={path}")
                    return True
                print("[step34] ChatGPT 会话 token 校验失败")
            return False

        # 第35步：交换 token。
        print_step(35, "交换 access_token 和 refresh_token")
        token_data = await exchange_code_for_tokens(result["code"], verifier)
        if not token_data:
            return False

        # 第36步：保存账号信息和 token。
        print_step(36, "保存账号信息和 token 文件")
        token_data["account_password"] = password
        token_data["mail_token"] = mail_token
        path = save_tokens(email, token_data)
        print(f"Success, saved token: {path}")
        log.info(f"success: email={email}, worker_id={worker_id}, path={path}")
        return True
    finally:
        # 第37步：关闭浏览器并清理进程。
        print_step(37, "关闭浏览器并清理进程")
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        stop_process_tree(process)
        cleanup_profile_dir(profile_dir)


async def main() -> int:
    print(f"Run count: {'infinite' if RUN_COUNT == 0 else RUN_COUNT}")
    print(f"Run interval range: {MIN_INTERVAL}~{MAX_INTERVAL}s")
    print(f"Worker range: {WORKER_ID_MIN}~{WORKER_ID_MAX}")
    print(f"Concurrency: {CONCURRENCY}")
    print(f"TempMail API: {TEMPMAIL_BASE_URL}")
    loop = asyncio.get_running_loop()
    servers = start_oauth_server(loop)
    success_count = 0
    fail_count = 0
    try:
        async with async_playwright() as playwright:
            counter_lock = asyncio.Lock()
            run_index = 0

            async def worker_loop(worker_no: int) -> None:
                nonlocal run_index, success_count, fail_count
                while True:
                    async with counter_lock:
                        if RUN_COUNT != 0 and run_index >= RUN_COUNT:
                            return
                        run_index += 1
                        round_no = run_index
                    try:
                        ok = await register_one(playwright)
                        async with counter_lock:
                            success_count += 1 if ok else 0
                            fail_count += 0 if ok else 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"Worker {worker_no} round {round_no} failed: {exc}")
                        log.error(f"worker {worker_no} round {round_no} failed:\n{traceback.format_exc()}")
                        # Try rotating proxy on network failure
                        if rotate_proxy_on_failure():
                            print(f"Worker {worker_no} rotated to next proxy, retrying...")
                            time.sleep(2)
                            try:
                                ok = await register_one(playwright)
                                async with counter_lock:
                                    success_count += 1 if ok else 0
                                    fail_count += 0 if ok else 1
                            except Exception as retry_exc:
                                print(f"Worker {worker_no} retry after proxy rotation failed: {retry_exc}")
                                log.error(f"worker {worker_no} retry failed:\n{traceback.format_exc()}")
                                async with counter_lock:
                                    fail_count += 1
                        else:
                            async with counter_lock:
                                fail_count += 1
                    if MAX_INTERVAL > 0 and (RUN_COUNT == 0 or round_no < RUN_COUNT):
                        interval = random.randint(max(0, MIN_INTERVAL), MAX_INTERVAL)
                        print(f"Worker {worker_no} wait {interval}s before next round")
                        await asyncio.sleep(interval)

            workers = [asyncio.create_task(worker_loop(i + 1)) for i in range(CONCURRENCY)]
            await asyncio.gather(*workers)
    finally:
        for server in servers:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        print(f"Completed. Success: {success_count}, Failed: {fail_count}")

 if fail_count >0 and success_count ==0:
 return1
 return0


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
