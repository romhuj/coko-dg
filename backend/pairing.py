# -*- coding: utf-8 -*-
"""DG-LAB V4 配对地址；控制端与手机必须连接同一中继实例。"""
import ipaddress
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


def pairing_relay_url(cfg, lan_ip: str) -> str:
    """保留中继协议、端口和路径，仅把本机监听地址换成手机可访问的 IP。"""
    relay = cfg["relay"]
    public_url = str(relay.get("public_url") or "").strip()
    base = public_url or str(relay["url"]).strip()
    parts = urlsplit(base)
    host = parts.hostname or ""
    local = host.lower().rstrip(".") == "localhost"
    try:
        address = ipaddress.ip_address(host)
        local = local or address.is_loopback or address.is_unspecified
    except ValueError:
        pass
    if local:
        phone_host = str(lan_ip).strip().strip("[]")
        if ":" in phone_host:
            phone_host = f"[{phone_host}]"
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
        port = f":{parts.port}" if parts.port is not None else ""
        parts = parts._replace(netloc=f"{userinfo}{phone_host}{port}")
    return urlunsplit(parts._replace(fragment=""))


def build_pair_url(cfg, lan_ip: str, controller_id: str) -> str:
    """生成官方 V4 Socket 二维码，替换旧配对 ID 并保留其它查询参数。"""
    parts = urlsplit(pairing_relay_url(cfg, lan_ip))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"tid", "targetId"}
    ]
    query.append(("tid", controller_id))
    ws_url = urlunsplit(parts._replace(query=urlencode(query)))
    return "https://dungeon-lab.cn/s/?v=1&action=socket&url=" + quote(ws_url, safe="")
