from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_SUBSCRIPTION_URL = "https://henryli777.github.io/google_ssr_actions/sub/top100.txt"


def _single(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    if not values:
        return default
    return values[0]


def _decode_tag(line: str, index: int) -> str:
    parsed = urlparse(line)
    if parsed.fragment:
        return unquote(parsed.fragment)
    return f"node-{index}"


def _parse_ss(line: str, tag: str) -> dict[str, Any]:
    body = line[5:]
    main = body.split("#", 1)[0]
    userinfo, hostport = main.rsplit("@", 1)
    padded = userinfo + "=" * (-len(userinfo) % 4)
    decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
    method, password = decoded.split(":", 1)
    host, port = hostport.rsplit(":", 1)
    return {
        "type": "shadowsocks",
        "tag": tag,
        "server": host,
        "server_port": int(port),
        "method": method,
        "password": password,
    }


def _build_tls(params: dict[str, list[str]], server_name_keys: tuple[str, ...], insecure_keys: tuple[str, ...]) -> dict[str, Any]:
    server_name = ""
    for key in server_name_keys:
        value = _single(params, key)
        if value:
            server_name = value
            break

    insecure = False
    for key in insecure_keys:
        value = _single(params, key).lower()
        if value in {"1", "true", "yes"}:
            insecure = True
            break

    tls: dict[str, Any] = {"enabled": True}
    if server_name:
        tls["server_name"] = server_name
    if insecure:
        tls["insecure"] = True
    return tls


def _parse_vless(line: str, tag: str) -> dict[str, Any]:
    parsed = urlparse(line)
    params = parse_qs(parsed.query, keep_blank_values=True)
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": tag,
        "server": parsed.hostname,
        "server_port": parsed.port,
        "uuid": unquote(parsed.username or ""),
        "network": _single(params, "type", "tcp"),
        "tls": _build_tls(params, ("sni",), ()),
    }
    flow = _single(params, "flow")
    if flow:
        outbound["flow"] = flow

    transport_type = outbound["network"]
    if transport_type == "ws":
        headers = {}
        host = _single(params, "host")
        if host:
            headers["Host"] = host
        outbound["transport"] = {
            "type": "ws",
            "path": _single(params, "path", "/") or "/",
            "headers": headers,
        }
    elif transport_type == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": _single(params, "serviceName"),
        }
    return outbound


def _parse_trojan(line: str, tag: str) -> dict[str, Any]:
    parsed = urlparse(line)
    params = parse_qs(parsed.query, keep_blank_values=True)
    outbound: dict[str, Any] = {
        "type": "trojan",
        "tag": tag,
        "server": parsed.hostname,
        "server_port": parsed.port,
        "password": unquote(parsed.username or ""),
        "tls": _build_tls(params, ("sni", "peer"), ("allowInsecure",)),
    }

    fingerprint = _single(params, "fp")
    if fingerprint:
        outbound["tls"]["utls"] = {"enabled": True, "fingerprint": fingerprint}

    transport_type = _single(params, "type")
    if transport_type == "ws":
        host = _single(params, "host") or _single(params, "peer")
        headers = {"Host": host} if host else {}
        outbound["transport"] = {
            "type": "ws",
            "path": _single(params, "path", "/") or "/",
            "headers": headers,
        }
    return outbound


def _parse_hysteria2(line: str, tag: str) -> dict[str, Any]:
    parsed = urlparse(line)
    params = parse_qs(parsed.query, keep_blank_values=True)
    outbound: dict[str, Any] = {
        "type": "hysteria2",
        "tag": tag,
        "server": parsed.hostname,
        "server_port": parsed.port,
        "password": unquote(parsed.username or ""),
        "tls": _build_tls(params, ("sni",), ("insecure",)),
    }

    mport = _single(params, "mport")
    if mport:
        outbound.pop("server_port", None)
        ranges = []
        for item in mport.split(","):
            item = item.strip()
            if not item:
                continue
            if "-" in item and ":" not in item:
                start, end = item.split("-", 1)
                item = f"{start}:{end}"
            ranges.append(item)
        if ranges:
            outbound["server_ports"] = ranges

    obfs_type = _single(params, "obfs")
    obfs_password = _single(params, "obfs-password")
    if obfs_type:
        outbound["obfs"] = {
            "type": obfs_type,
            "password": obfs_password,
        }

    return outbound


def build_outbound(line: str, index: int) -> dict[str, Any] | None:
    scheme = line.split("://", 1)[0]
    tag = f"{index:03d}-{_decode_tag(line, index)}"
    if scheme == "ss":
        return _parse_ss(line, tag)
    if scheme == "vless":
        return _parse_vless(line, tag)
    if scheme == "trojan":
        return _parse_trojan(line, tag)
    if scheme == "hysteria2":
        return _parse_hysteria2(line, tag)
    return None


def fetch_subscription(url: str) -> list[str]:
    req = Request(url, headers={"User-Agent": "codex", "Accept": "text/plain,*/*"})
    try:
        with urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
    except Exception:
        result = subprocess.run(
            ["curl", "-fsSL", url],
            check=True,
            capture_output=True,
            text=True,
        )
        text = result.stdout
    return [line.strip() for line in text.splitlines() if line.strip()]


def sanitize_tag(value: str) -> str:
    keep = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_", ".", " "}:
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip().replace(" ", "_") or "node"


def build_config(lines: list[str], listen_host: str, listen_port: int, test_url: str) -> dict[str, Any]:
    node_tags: list[str] = []
    outbounds: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        outbound = build_outbound(line, index)
        if not outbound:
            continue
        outbound["tag"] = sanitize_tag(str(outbound["tag"]))
        node_tags.append(outbound["tag"])
        outbounds.append(outbound)

    if not outbounds:
        raise SystemExit("No supported nodes found in subscription")

    outbounds.extend(
        [
            {
                "type": "urltest",
                "tag": "auto",
                "outbounds": node_tags,
                "url": test_url,
                "interval": "3m",
                "tolerance": 50,
                "interrupt_exist_connections": True,
            },
            {"type": "direct", "tag": "direct"},
        ]
    )

    return {
        "log": {"level": "info"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": listen_host,
                "listen_port": listen_port,
            }
        ],
        "outbounds": outbounds,
        "route": {
            "auto_detect_interface": True,
            "final": "auto",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription-url", default=DEFAULT_SUBSCRIPTION_URL)
    parser.add_argument("--output", default="sing-box-proxy.json")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=7897)
    parser.add_argument("--max-nodes", type=int, default=20)
    parser.add_argument("--test-url", default=DEFAULT_TEST_URL)
    args = parser.parse_args()

    lines = fetch_subscription(args.subscription_url)
    if args.max_nodes > 0:
        lines = lines[: args.max_nodes]

    config = build_config(lines, args.listen_host, args.listen_port, args.test_url)
    output_path = Path(args.output)
    output_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"subscription_url={args.subscription_url}")
    print(f"node_count={len(config['outbounds']) - 2}")
    print(f"listen={args.listen_host}:{args.listen_port}")
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
