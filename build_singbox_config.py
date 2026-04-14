from __future__ import annotations

import argparse
import base64
import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
DEFAULT_SUBSCRIPTION_URL = "https://henryli777.github.io/google_ssr_actions/sub/top100.yaml"

SubscriptionEntry = str | dict[str, Any]


def _single(params: dict[str, list[str]], key: str, default: str = "") -> str:
    values = params.get(key)
    if not values:
        return default
    return values[0]


def _stringify(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _boolish(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _int_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _normalize_proxy_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "hy2":
        return "hysteria2"
    if normalized == "shadowsocks":
        return "ss"
    return normalized


def _decode_tag(line: str, index: int) -> str:
    parsed = urlparse(line)
    if parsed.fragment:
        return unquote(parsed.fragment)
    return f"node-{index}"


def _entry_name(entry: SubscriptionEntry, index: int) -> str:
    if isinstance(entry, dict):
        name = _stringify(entry.get("name"))
        if name:
            return name
        return f"node-{index}"
    return _decode_tag(entry, index)


def _entry_scheme(entry: SubscriptionEntry) -> str:
    if isinstance(entry, dict):
        return _normalize_proxy_type(_stringify(entry.get("type")))
    return entry.split("://", 1)[0].lower()


def _entry_host(entry: SubscriptionEntry) -> str:
    if isinstance(entry, dict):
        return _stringify(entry.get("server")).lower()
    return (urlparse(entry).hostname or "").lower()


def rank_subscription_entries(entries: list[SubscriptionEntry]) -> list[SubscriptionEntry]:
    ranked: list[tuple[int, int, SubscriptionEntry]] = []

    for index, entry in enumerate(entries, start=1):
        scheme = _entry_scheme(entry)
        host = _entry_host(entry)
        tag = _entry_name(entry, index).lower()
        score = 0

        if any(keyword in tag for keyword in ("gpt", "chatgpt", "openai")):
            score += 100

        if scheme in {"ss", "shadowsocks"}:
            score += 40
        elif scheme == "trojan":
            score += 30
        elif scheme == "hysteria2":
            score += 10
        elif scheme == "vless":
            score += 5

        if host.endswith("network-cdn-gw-yd.net"):
            score += 30
        if host.endswith("aikunapp.com"):
            score += 20
        if host.endswith("421421.xyz"):
            score += 15

        # These providers consistently failed ChatGPT/OpenAI validation in CI.
        if host.endswith("the-best-airport.com"):
            score -= 80
        if host.endswith("poke-mon.xyz"):
            score -= 80

        ranked.append((score, -index, entry))

    ranked.sort(reverse=True)
    return [entry for _, _, entry in ranked]


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


def _build_tls(
    params: dict[str, list[str]],
    server_name_keys: tuple[str, ...],
    insecure_keys: tuple[str, ...],
) -> dict[str, Any]:
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


def _alpn_list(value: Any) -> list[str]:
    if isinstance(value, list):
        result = []
        for item in value:
            text = _stringify(item)
            if text:
                result.append(text)
        return result
    text = _stringify(value)
    if not text:
        return []
    if "," in text:
        return [item.strip() for item in text.split(",") if item.strip()]
    return [text]


def _build_transport_from_clash(proxy: dict[str, Any]) -> dict[str, Any] | None:
    network = _stringify(proxy.get("network"), "tcp").lower() or "tcp"

    if network == "ws":
        headers: dict[str, str] = {}
        path = "/"
        ws_opts = proxy.get("ws-opts")
        if isinstance(ws_opts, dict):
            path = _stringify(ws_opts.get("path"), "/") or "/"
            raw_headers = ws_opts.get("headers")
            if isinstance(raw_headers, dict):
                for key, value in raw_headers.items():
                    header_name = _stringify(key)
                    header_value = _stringify(value)
                    if header_name and header_value:
                        headers[header_name] = header_value

        host = _stringify(proxy.get("host"))
        if host and "Host" not in headers:
            headers["Host"] = host

        return {
            "type": "ws",
            "path": path,
            "headers": headers,
        }

    if network == "grpc":
        service_name = ""
        grpc_opts = proxy.get("grpc-opts")
        if isinstance(grpc_opts, dict):
            service_name = _stringify(grpc_opts.get("grpc-service-name")) or _stringify(grpc_opts.get("serviceName"))

        transport: dict[str, Any] = {"type": "grpc"}
        if service_name:
            transport["service_name"] = service_name
        return transport

    return None


def _build_tls_from_clash(proxy: dict[str, Any], *, default_enabled: bool = False) -> dict[str, Any] | None:
    server_name = _stringify(proxy.get("servername")) or _stringify(proxy.get("sni"))
    insecure = _boolish(proxy.get("skip-cert-verify")) or _boolish(proxy.get("allowInsecure"))
    enabled = _boolish(proxy.get("tls"), default_enabled)
    alpn = _alpn_list(proxy.get("alpn"))
    fingerprint = _stringify(proxy.get("client-fingerprint")) or _stringify(proxy.get("fingerprint"))
    reality_opts = proxy.get("reality-opts")

    if not enabled and not server_name and not insecure and not alpn and not fingerprint and not isinstance(reality_opts, dict):
        return None

    tls: dict[str, Any] = {"enabled": True}
    if server_name:
        tls["server_name"] = server_name
    if insecure:
        tls["insecure"] = True
    if alpn:
        tls["alpn"] = alpn
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    if isinstance(reality_opts, dict):
        public_key = _stringify(reality_opts.get("public-key"))
        short_id = _stringify(reality_opts.get("short-id"))
        if public_key or short_id:
            reality: dict[str, Any] = {"enabled": True}
            if public_key:
                reality["public_key"] = public_key
            if short_id:
                reality["short_id"] = short_id
            tls["reality"] = reality
    return tls


def _build_outbound_from_clash(proxy: dict[str, Any], index: int) -> dict[str, Any] | None:
    proxy_type = _normalize_proxy_type(_stringify(proxy.get("type")))
    tag = f"{index:03d}-{_entry_name(proxy, index)}"
    server = _stringify(proxy.get("server"))
    server_port = _int_value(proxy.get("port"))

    if not server or server_port is None:
        return None

    if proxy_type == "vless":
        uuid = _stringify(proxy.get("uuid"))
        if not uuid:
            return None
        network = _stringify(proxy.get("network"), "tcp").lower() or "tcp"
        outbound: dict[str, Any] = {
            "type": "vless",
            "tag": tag,
            "server": server,
            "server_port": server_port,
            "uuid": uuid,
            "network": network,
        }
        flow = _stringify(proxy.get("flow"))
        if flow:
            outbound["flow"] = flow
        packet_encoding = _stringify(proxy.get("packet-encoding"))
        if packet_encoding:
            outbound["packet_encoding"] = packet_encoding
        tls = _build_tls_from_clash(proxy, default_enabled=_boolish(proxy.get("tls")))
        if tls:
            outbound["tls"] = tls
        transport = _build_transport_from_clash(proxy)
        if transport:
            outbound["transport"] = transport
        return outbound

    if proxy_type == "trojan":
        password = _stringify(proxy.get("password"))
        if not password:
            return None
        outbound = {
            "type": "trojan",
            "tag": tag,
            "server": server,
            "server_port": server_port,
            "password": password,
            "tls": _build_tls_from_clash(proxy, default_enabled=True) or {"enabled": True},
        }
        transport = _build_transport_from_clash(proxy)
        if transport:
            outbound["transport"] = transport
        return outbound

    if proxy_type == "hysteria2":
        password = _stringify(proxy.get("password"))
        if not password:
            return None
        outbound = {
            "type": "hysteria2",
            "tag": tag,
            "server": server,
            "server_port": server_port,
            "password": password,
            "tls": _build_tls_from_clash(proxy, default_enabled=True) or {"enabled": True},
        }
        obfs_type = _stringify(proxy.get("obfs"))
        obfs_password = _stringify(proxy.get("obfs-password"))
        if obfs_type:
            outbound["obfs"] = {
                "type": obfs_type,
                "password": obfs_password,
            }
        return outbound

    if proxy_type == "ss":
        method = _stringify(proxy.get("cipher")) or _stringify(proxy.get("method"))
        password = _stringify(proxy.get("password"))
        plugin = _stringify(proxy.get("plugin"))
        if not method or not password:
            return None
        if plugin:
            return None
        return {
            "type": "shadowsocks",
            "tag": tag,
            "server": server,
            "server_port": server_port,
            "method": method,
            "password": password,
        }

    return None


def build_outbound(entry: SubscriptionEntry, index: int) -> dict[str, Any] | None:
    if isinstance(entry, dict):
        return _build_outbound_from_clash(entry, index)

    scheme = entry.split("://", 1)[0]
    tag = f"{index:03d}-{_decode_tag(entry, index)}"
    if scheme == "ss":
        return _parse_ss(entry, tag)
    if scheme == "vless":
        return _parse_vless(entry, tag)
    if scheme == "trojan":
        return _parse_trojan(entry, tag)
    if scheme == "hysteria2":
        return _parse_hysteria2(entry, tag)
    return None


def fetch_subscription_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "codex", "Accept": "text/plain,*/*"})
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        result = subprocess.run(
            ["curl", "-fsSL", url],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout


def _parse_yaml_scalar(value: str) -> Any:
    text = value.strip()
    if not text:
        return ""
    if text[:1] == text[-1:] and text[:1] in {'"', "'"}:
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _parse_clash_proxies_from_text(text: str) -> list[dict[str, Any]]:
    if yaml:
        try:
            parsed = yaml.safe_load(text)
        except Exception:
            parsed = None
        proxies = parsed.get("proxies") if isinstance(parsed, dict) else None
        if isinstance(proxies, list):
            return [item for item in proxies if isinstance(item, dict)]

    proxies: list[dict[str, Any]] = []
    in_proxies = False
    current: dict[str, Any] | None = None
    current_indent = 0

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if not in_proxies:
            if stripped == "proxies:":
                in_proxies = True
            continue

        if indent == 0 and stripped.endswith(":") and stripped != "proxies:":
            break

        if stripped.startswith("- "):
            if current:
                proxies.append(current)
            current = {}
            current_indent = indent
            head = stripped[2:].strip()
            if ":" in head:
                key, value = head.split(":", 1)
                current[key.strip()] = _parse_yaml_scalar(value)
            continue

        if current is None or indent <= current_indent:
            continue
        if ":" not in stripped:
            continue

        key, value = stripped.split(":", 1)
        current[key.strip()] = _parse_yaml_scalar(value)

    if current:
        proxies.append(current)

    return proxies


def parse_subscription_entries(text: str) -> list[SubscriptionEntry]:
    if "proxies:" in text:
        proxies = _parse_clash_proxies_from_text(text)
        if proxies:
            return proxies
    return [line.strip() for line in text.splitlines() if line.strip()]


def fetch_subscription(url: str) -> list[SubscriptionEntry]:
    return parse_subscription_entries(fetch_subscription_text(url))


def sanitize_tag(value: str) -> str:
    keep = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_", ".", " "}:
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip().replace(" ", "_") or "node"


def build_config(entries: list[SubscriptionEntry], listen_host: str, listen_port: int, test_url: str) -> dict[str, Any]:
    node_tags: list[str] = []
    outbounds: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        outbound = build_outbound(entry, index)
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

    entries = fetch_subscription(args.subscription_url)
    entries = rank_subscription_entries(entries)
    if args.max_nodes > 0:
        entries = entries[: args.max_nodes]

    config = build_config(entries, args.listen_host, args.listen_port, args.test_url)
    output_path = Path(args.output)
    output_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"subscription_url={args.subscription_url}")
    print(f"node_count={len(config['outbounds']) - 2}")
    print(f"listen={args.listen_host}:{args.listen_port}")
    print(f"output={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
