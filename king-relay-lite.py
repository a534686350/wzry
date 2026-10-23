#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
king-relay-lite —— 强哥公益雷达同款中转服务（独立实现，无授权校验）

协议（抓包自 king.weilua.top 1.2.8）：
  网页/后台 -> :8080（WEB_PORT，纯静态 + 管理 API）
  浏览器/APP -> 服务端 :8888/ws    数据通道
      "subscribeHome"            订阅房间列表
      "subscribe[==]<roomId>"    订阅指定房间
      "ping##<n>:<ts>"           心跳
  服务端 -> 浏览器
      "homeData##<id>[==]<viewers>,<id>[==]<viewers>,..."
      "gameData##<英雄数据>"      200ms 推送，段间 [==]
      "pong##<n>:<ts>"

  上报端（游戏客户端） -> 服务端 :9999
      "gameData[==]<roomId>[==]<target>[==]<payload>"    上报/查询
      旧格式 "<roomId>[==]<target>[==]<payload>"
"""
import asyncio
import base64
import hashlib
import json
import os
import posixpath
import random
import re
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LICENSE_FILE = os.path.join(DATA_DIR, "license.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

WS_PORT = int(os.environ.get("WS_PORT", "8888"))
TCP_PORT = int(os.environ.get("TCP_PORT", "9999"))
# 网页/后台走独立端口，和游戏数据通道（8888 WS / 9999 TCP）彻底分开
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(BASE_DIR, "dist"))
APP_FILE = os.environ.get("APP_FILE", os.path.join(STATIC_DIR, "app.apk"))
PUSH_INTERVAL = float(os.environ.get("PUSH_INTERVAL", "0.2"))
ROOM_TTL = float(os.environ.get("ROOM_TTL", "60"))
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

# roomId -> {"data": str, "ts": float, "viewers": int, "target": str}
ROOMS = {}
# target -> {"data", "ts"} 定向数据
DMS = {}
_SAMPLE = [0]
DUMP_TS = {}
# 今日流量统计（服务端下发 / 上报端上送）
STATS = {"day": time.strftime("%Y-%m-%d"), "up": 0, "down": 0}

# ---------------------------------------------------------------- 配置与授权
DEFAULT_CONFIG = {
    "site_name": "强哥公益雷达",
    # 子站模式：True 时后台与自助部署全部关闭，只留前台展示
    "child_mode": False,
    # 前台默认数据源（IP:端口 或 域名:端口）；留空=只连本机
    "default_source": "",
    "admin_user": "admin",
    # 留空 = 首次启动自动生成随机密码并写入日志；也可直接填固定密码
    "admin_pass": "",
    # 空 = 本机为母站，不校验；部署端填授权服务器地址 + 授权码
    "license_server": "",
    "license_code": "",
    # 母站对外访问地址（子站回连授权/镜像用）。留空 = 自动用 IP:8080
    "public_url": "",
    # 房间数据上游（host:port）。留空则自动用 license_server 的主机 + 8888。
    # 子站靠这个从母站镜像房间列表与对局数据，打开网页就有房间。
    "upstream": "",
    "upstream_on": True,
    # 公告（后台可改，前台 /api/announcement 实时读取）
    "announce_on": True,
    "announce_title": "系统公告",
    "announce_content": "<p>服务运行正常。</p>",
    # modal = 弹窗（带「我知道了」），banner = 顶部横幅
    "announce_mode": "modal",
    "announce_repeat": 120,
    "announce_updated": "",
    # 前台资源群弹窗（后台可改，也支持通过「部署记录」远程推送到子站）
    "popup_on": True,
    "popup_eyebrow": "QIANG GE · PUBLIC RADAR",
    "popup_title": "强哥<em>公益雷达</em>分享",
    "popup_desc": "进群获取更多资源：最新客户端、战术地图数据、版本更新与使用答疑，群内同步。",
    "popup_group": "862938931",
    "popup_tip": "加群请备注「强哥公益雷达」",
    # 0 = 每次打开都弹；1 = 一天一次；n = n 天一次
    "popup_repeat_days": 1,
}
CONFIG = dict(DEFAULT_CONFIG)
# 全局免授权开关：True = 彻底不要授权体系
#   · 部署卡密不校验（/deploy 随便填或留空都行）
#   · 子站不向母站校验 license_server/license_code
#   · 没有「未授权」拦截页，TCP 上报口照常开放
# 想恢复授权体系改成 False 即可，其它代码不用动。
LICENSE_OFF = True
LICENSE_STATE = {"ok": True, "reason": "", "expire": ""}
SESSIONS = {}
SESSION_TTL = 12 * 3600
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def load_config():
    global CONFIG
    CONFIG = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            CONFIG.update(d)
    except Exception:
        pass
    return CONFIG


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log("写配置失败:", repr(e))
        return False


def license_load():
    try:
        with open(LICENSE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("codes"), list):
            return d
    except Exception:
        pass
    return {"codes": []}


def license_save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LICENSE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def check_deploy_code(code):
    """校验部署卡密。返回 (错误信息, 卡密记录)。"""
    code = (code or "").strip().upper()
    if LICENSE_OFF:
        return "", {"code": code or "FREE", "kind": "deploy", "expire": "",
                    "enabled": True, "note": "免授权"}
    if not code:
        return "请填写部署卡密", None
    try:
        d = license_load()
    except Exception:
        return "卡密库读取失败", None
    for c in d.get("codes", []):
        if str(c.get("code", "")).strip().upper() == code:
            if not c.get("enabled", True):
                return "卡密已停用", None
            if (c.get("kind") or "site") != "deploy":
                return "这不是部署卡密", None
            exp = (c.get("expire") or "").strip()
            if exp:
                today = time.strftime("%Y-%m-%d")
                if exp < today:
                    return "卡密已过期（%s）" % exp, None
            _mu = int(c.get("max_uses") or 0)
            if _mu > 0 and int(c.get("used_count") or 0) >= _mu:
                return "卡密已用完（上限 %d 次）" % _mu, None
            return "", c
    return "卡密不存在", None


def mark_deploy_code_used(code, host):
    """部署成功后把卡密标记为已用。"""
    code = (code or "").strip().upper()
    if not code:
        return
    try:
        d = license_load()
        for c in d.get("codes", []):
            if str(c.get("code", "")).strip().upper() == code:
                c["used_count"] = int(c.get("used_count") or 0) + 1
                c["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
                _ips = c.get("ips")
                if not isinstance(_ips, list):
                    _ips = []
                if host and str(host) not in _ips:
                    _ips.append(str(host))
                c["ips"] = _ips[-30:]
                c["note"] = ((c.get("note") or "") + " → " + str(host)).strip(" →")
                break
        license_save(d)
    except Exception:
        pass


def gen_license_code():
    return "KV-" + "-".join(
        "".join(random.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(3))


def license_verify(code, host="", ip=""):
    """返回 (ok, reason, expire)；host 为自报主机名，ip 为请求来源 IP（两者任一命中绑定即可）。"""
    if LICENSE_OFF:
        return True, "", ""
    code = (code or "").strip().upper()
    host = (host or "").strip()
    ip = (ip or "").strip()
    if not code:
        return False, "缺少授权码", ""
    d = license_load()
    for it in d["codes"]:
        if str(it.get("code", "")).strip().upper() != code:
            continue
        if not it.get("enabled", True):
            return False, "授权码已停用", it.get("expire", "")
        exp = (it.get("expire") or "").strip()
        if exp and exp < time.strftime("%Y-%m-%d"):
            return False, "授权码已过期", exp
        bind = (it.get("host") or "").strip().lower()
        cand = (host + " " + ip).strip().lower()
        if bind and cand and bind not in cand:
            return False, "授权域名/IP 不匹配", exp
        it["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        it["last_host"] = host
        it["used_count"] = int(it.get("used_count", 0)) + 1
        license_save(d)
        return True, "", exp
    return False, "授权码不存在", ""


def local_ip():
    """取本机对外网卡 IP（用于授权码绑定 IP 的校验）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.8)
        s.connect(("114.114.114.114", 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def remote_license_check():
    if LICENSE_OFF:
        return True, "", ""
    srv = str(CONFIG.get("license_server") or "").strip()
    code = str(CONFIG.get("license_code") or "").strip()
    if not srv:
        return True, "", ""
    if not code:
        return False, "未填写授权码", ""
    q = urllib.parse.urlencode({"code": code, "host": local_ip() or socket.gethostname(),
                                "hostname": socket.gethostname(),
                                "app": "king-vision-lite"})
    url = srv.rstrip("/") + "/api/license/verify?" + q
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        ok = str(d.get("result", "")).lower() == "ok"
        return ok, d.get("reason", ""), d.get("expireAt", "")
    except Exception as e:
        return False, "授权服务器不可达（%s）" % e, ""


def new_session(user):
    tok = hashlib.sha256(
        (user + str(time.time()) + os.urandom(8).hex()).encode()).hexdigest()[:32]
    SESSIONS[tok] = time.time() + SESSION_TTL
    return tok


def session_ok(tok):
    exp = SESSIONS.get(tok or "")
    if not exp:
        return False
    if exp < time.time():
        SESSIONS.pop(tok, None)
        return False
    return True


def get_cookie(headers, name):
    raw = headers.get("cookie", "")
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == name:
                return v.strip()
    return ""


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def room_viewers(room):
    r = ROOMS.get(room)
    if not r:
        return 0
    if time.time() - r["ts"] > ROOM_TTL:
        return 0
    n = sum(1 for c in WS_CLIENTS if c.room == room)
    return max(n, r.get("viewers", 1) or 1)


def home_payload():
    items = [k for k, v in ROOMS.items() if time.time() - v["ts"] <= ROOM_TTL]
    items.sort(key=lambda x: (len(x), x))
    return "homeData##" + ",".join("%s[==]%d" % (k, room_viewers(k)) for k in items)


class WSConn:
    __slots__ = ("writer", "room", "home", "alive", "buf")

    def __init__(self, writer):
        self.writer = writer
        self.room = None
        self.home = False
        self.alive = True
        self.buf = b""


async def ws_send(writer, text):
    payload = text.encode()
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x81, 126, n)
    else:
        head = struct.pack("!BBQ", 0x81, 127, n)
    writer.write(head + payload)
    await writer.drain()


# ---------------- WebSocket 反向代理 ----------------
def _ws_pack(op, data, mask=False):
    n = len(data)
    b1 = 0x80 | op
    m = 0x80 if mask else 0
    if n < 126:
        head = struct.pack("!BB", b1, m | n)
    elif n < 65536:
        head = struct.pack("!BBH", b1, m | 126, n)
    else:
        head = struct.pack("!BBQ", b1, m | 127, n)
    if mask:
        mk = os.urandom(4)
        body = bytes(b ^ mk[i & 3] for i, b in enumerate(data))
        return head + mk + body
    return head + data


async def _ws_read(reader):
    h = await reader.readexactly(2)
    b1, b2 = h[0], h[1]
    op = b1 & 0x0F
    masked = b2 & 0x80
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", await reader.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", await reader.readexactly(8))[0]
    mk = await reader.readexactly(4) if masked else None
    data = bytearray(await reader.readexactly(ln)) if ln else bytearray()
    if masked and mk:
        for i in range(len(data)):
            data[i] ^= mk[i & 3]
    return op, bytes(data)


async def _ws_connect(host, port, path, origin):
    r, w = await asyncio.open_connection(host, port)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: %s\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36\r\n"
        "\r\n"
    ) % (path, host, port, key, origin)
    w.write(req.encode())
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=10)
    first = head.split(b"\r\n", 1)[0].decode("latin1", "ignore")
    if " 101 " not in first + " ":
        raise RuntimeError("target refused: " + first[:100])
    return r, w


async def _ws_pump(src, dst, to_client):
    """to_client=True: 目标->浏览器（不 mask）；False: 浏览器->目标（mask）。"""
    try:
        while True:
            op, data = await _ws_read(src)
            if op == 0x8:
                try:
                    dst.write(_ws_pack(0x8, data, mask=not to_client))
                    await dst.drain()
                except Exception:
                    pass
                return
            if op == 0x9:
                dst.write(_ws_pack(0xA, data, mask=not to_client))
                await dst.drain()
                continue
            if op == 0xA:
                continue
            dst.write(_ws_pack(op if op in (0x1, 0x2) else 0x1, data, mask=not to_client))
            await dst.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, asyncio.TimeoutError):
        pass
    except Exception as e:
        log("proxy pump:", repr(e))


async def handle_proxy_ws(reader, writer, headers, query):
    """浏览器 <-> 本站 <-> 外部 WS 源，帧级透传。"""
    q = urllib.parse.parse_qs(query or "")
    target = (q.get("target", [""])[0] or "").strip()
    tpath = (q.get("path", ["/ws"])[0] or "/ws").strip()
    if not tpath.startswith("/"):
        tpath = "/ws"
    if not target:
        try:
            writer.close()
        except Exception:
            pass
        return
    host, _, port_s = target.partition(":")
    try:
        port = int(port_s or 8888)
    except Exception:
        port = 8888

    # 1) 与浏览器握手
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        ).encode()
    )
    await writer.drain()

    # 2) 连目标（Origin 伪装成目标自身，绕开同源校验）
    tr = tw = None
    try:
        tr, tw = await _ws_connect(host, port, tpath, "http://%s:%d" % (host, port))
        log("代理建立 %s:%d%s" % (host, port, tpath))
        await asyncio.gather(
            _ws_pump(reader, tw, False),
            _ws_pump(tr, writer, True),
            return_exceptions=True,
        )
    except Exception as e:
        log("代理失败 %s:%d %s" % (host, port, repr(e)[:160]))
        try:
            writer.write(_ws_pack(0x8, b"", mask=False))
            await writer.drain()
        except Exception:
            pass
    finally:
        for w_ in (tw, writer):
            try:
                if w_:
                    w_.close()
            except Exception:
                pass
        log("代理关闭 %s:%d" % (host, port))



WS_CLIENTS = set()


async def ws_push_loop():
    while True:
        await asyncio.sleep(PUSH_INTERVAL)
        now = time.time()
        if not WS_CLIENTS:
            continue
        home_cache = None
        for c in list(WS_CLIENTS):
            try:
                if c.home:
                    if home_cache is None:
                        home_cache = home_payload()
                    await ws_send(c.writer, home_cache)
                if c.room:
                    r = ROOMS.get(c.room)
                    if r and r.get("data") and now - r["ts"] <= ROOM_TTL:
                        await ws_send(c.writer, "gameData##" + r["data"])
            except Exception:
                WS_CLIENTS.discard(c)
                try:
                    c.writer.close()
                except Exception:
                    pass


# ---------------- 上游镜像：子站自动同步母站的房间列表与对局数据 ----------------
# 子站自己不采集，房间列表完全靠「有人上报」。一键部署出来的新站点没有采集端，
# 网页打开就是 0 个房间。这里让子站主动连母站 WS，镜像房间列表并按需拉对局数据。
UP_LINK = {"host": "", "port": 0, "room": None, "ok": False, "rid": 0}
UP_ROOMS = set()


def upstream_target():
    """返回 (host, port)；母站自身或未配置时返回 None。"""
    if not CONFIG.get("upstream_on", True):
        return None
    raw = str(CONFIG.get("upstream") or "").strip()
    if not raw:
        raw = str(CONFIG.get("license_server") or "").strip()
    if not raw:
        return None
    s = raw.replace("https://", "").replace("http://", "").strip()
    s = s.split("/")[0].strip()
    port = 8888
    if s.count(":") == 1:
        h, p = s.rsplit(":", 1)
        if p.isdigit():
            s, port = h, int(p)
    if not s:
        return None
    if s in ("127.0.0.1", "localhost", "0.0.0.0") and port == WS_PORT:
        return None
    return s, port


async def up_handshake(host, port):
    reader, writer = await asyncio.open_connection(host, port, limit=1 << 20)
    key = base64.b64encode(os.urandom(16)).decode()
    writer.write((
        "GET /ws HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (host, port, key)).encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    first = head.split(b"\r\n")[0]
    if b"101" not in first:
        try:
            writer.close()
        except Exception:
            pass
        raise RuntimeError("上游握手失败 %r" % first[:60])
    return reader, writer


async def up_send(writer, text):
    p = text.encode()
    n = len(p)
    mask = os.urandom(4)
    if n < 126:
        head = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        head = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        head = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    body = bytes(b ^ mask[i & 3] for i, b in enumerate(p))
    writer.write(head + mask + body)
    await writer.drain()


async def up_recv(reader):
    h = await reader.readexactly(2)
    op = h[0] & 0x0F
    masked = h[1] & 0x80
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", await reader.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b"\x00\x00\x00\x00"
    data = bytearray(await reader.readexactly(ln)) if ln else bytearray()
    if masked:
        for i in range(len(data)):
            data[i] ^= mask[i & 3]
    if op == 0x8:
        raise asyncio.IncompleteReadError(b"", 1)
    if op not in (0x1, 0x2, 0x0):
        return ""
    return bytes(data).decode("utf-8", "ignore").strip()


def up_apply_home(payload):
    now = time.time()
    seen = set()
    for part in payload.split(","):
        part = part.strip()
        if not part:
            continue
        seg = part.split("[==]")
        rid = seg[0].strip()
        if not rid:
            continue
        try:
            vw = max(int(seg[1]), 1) if len(seg) > 1 else 1
        except Exception:
            vw = 1
        seen.add(rid)
        r = ROOMS.get(rid)
        if r is None:
            ROOMS[rid] = {"data": "", "ts": now, "viewers": vw, "target": "", "up": True}
        elif r.get("up"):
            r["ts"] = now
            r["viewers"] = vw
    UP_ROOMS.clear()
    UP_ROOMS.update(seen)
    for rid in list(ROOMS.keys()):
        r = ROOMS[rid]
        if r.get("up") and rid not in seen and not r.get("data"):
            ROOMS.pop(rid, None)


def up_apply_game(payload):
    rid = UP_LINK.get("room")
    if not rid or not payload:
        return
    r = ROOMS.get(rid)
    if r is None:
        r = {"data": "", "ts": time.time(), "viewers": 1, "target": "", "up": True}
        ROOMS[rid] = r
    r["data"] = payload
    r["ts"] = time.time()
    UP_LINK["rid"] = UP_LINK.get("rid", 0) + 1


def up_want_room():
    """本地有人在看哪个房间，就向上游要哪个房间的数据。"""
    for c in list(WS_CLIENTS):
        if c.room:
            return c.room
    return None


async def up_pump(reader, q):
    """独立读协程：绝不能被超时打断，否则会读走半个帧把流打乱。"""
    while True:
        msg = await up_recv(reader)
        if msg:
            q.put_nowait(msg)


async def upstream_loop():
    tgt = upstream_target()
    if not tgt:
        return
    host, port = tgt
    UP_LINK.update({"host": host, "port": port})
    log("上游镜像启用 → %s:%d" % (host, port))
    backoff = 3
    while True:
        writer = None
        try:
            reader, writer = await up_handshake(host, port)
            UP_LINK["ok"] = True
            backoff = 3
            log("上游镜像已连接 %s:%d" % (host, port))
            await up_send(writer, "getHome")
            q = asyncio.Queue()
            pump = _spawn(up_pump(reader, q))
            cur = None
            last_beat = time.time()
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        msg = ""
                    now = time.time()
                    if msg.startswith("homeData##"):
                        up_apply_home(msg[10:])
                        last_beat = now
                    elif msg.startswith("gameData##"):
                        up_apply_game(msg[10:])
                    want = up_want_room()
                    if want != cur:
                        cur = want
                        UP_LINK["room"] = cur
                        if cur:
                            await up_send(writer, "subscribe[==]" + cur)
                        await up_send(writer, "getHome")
                    elif now - last_beat > 5:
                        last_beat = now
                        await up_send(writer, "getHome")
                        if cur:
                            await up_send(writer, "web%d[==]%s" % (int(now * 1000), cur))
            finally:
                pump.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            UP_LINK["ok"] = False
            UP_LINK["room"] = None
            log("上游镜像断开：%r（%ds 后重连）" % (e, backoff))
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


# 去广告注入：屏蔽授权站/推荐位域名，隐藏站外推广链接，拦截 /support、/setup 路由
DEAD_LINK_RE = re.compile(r"seayun\.cn|ka\.?mi|/support|/setup", re.I)
AD_GUARD = r"""
<script>
(function(){
  /* 英雄头像改走同源代理，避免跨域图片污染 canvas 导致全屏地图变黑 */
  var HERO=/(?:https?:)?\/\/game\.gtimg\.cn\/images\/yxzj\/img201606\/heroimg\/(\d{1,4})\/(\d{1,4})\.jpg/i;
  var SKIN=/(?:https?:)?\/\/game\.gtimg\.cn\/images\/yxzj\/img201606\/skin\/hero-info\/(\d{1,4})\/[\d\-a-z]+\.jpg/i;
  function fixSrc(u){
    if(typeof u!=='string')return u;
    var s=u.match(SKIN);
    if(s)return '/skin/'+s[1]+'.jpg';
    var m=u.match(HERO);
    return m?('/hero/'+m[1]+'.jpg'):u;
  }
  try{
    var d=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
    if(d&&d.set){
      Object.defineProperty(HTMLImageElement.prototype,'src',{configurable:true,enumerable:true,
        get:function(){return d.get.call(this);},
        set:function(v){d.set.call(this,fixSrc(v));}});
    }
    var sa=Element.prototype.setAttribute;
    Element.prototype.setAttribute=function(n,v){
      if(this instanceof HTMLImageElement&&String(n).toLowerCase()==='src')v=fixSrc(v);
      return sa.call(this,n,v);
    };
  }catch(e){}
  var BAD=/seayun\.cn|plug\.seayun|(^|\/)support($|\/|\?)|(^|\/)setup($|\/|\?)/i;
  function blocked(u){try{return BAD.test(String(u&&u.url||u))}catch(e){return false}}
  try{var _f=window.fetch;window.fetch=function(u,o){
    if(blocked(u)){return Promise.resolve(new Response('{"code":0,"data":[]}',{status:200,headers:{'Content-Type':'application/json'}}));}
    return _f.apply(this,arguments);};}catch(e){}
  try{var _o=XMLHttpRequest.prototype.open,_s=XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open=function(m,u){try{this.__blk=blocked(u)}catch(e){}return _o.apply(this,arguments);};
    XMLHttpRequest.prototype.send=function(){if(this.__blk){try{this.abort()}catch(e){}return}return _s.apply(this,arguments);};}catch(e){}
  try{['pushState','replaceState'].forEach(function(k){var f=history[k];
    history[k]=function(s,t,u){if(typeof u==='string'&&/^\/(support|setup)/.test(u))u='/';return f.call(this,s,t,u);};});}catch(e){}
  function clean(){
    var el=document.querySelectorAll('a[href],img[src],iframe[src]');
    for(var i=0;i<el.length;i++){var n=el[i],h=n.getAttribute('href')||n.getAttribute('src')||'';
      if(BAD.test(h)){n.style.display='none';n.setAttribute('data-ad-blocked','1');}}
  }
  if(document.readyState!=='loading'){clean();}else{document.addEventListener('DOMContentLoaded',clean);}
  setInterval(clean,800);
  /* 去掉鸣谢功能：入口按钮与弹窗都不显示 */
  (function(){
    try{
      var st=document.createElement('style');
      st.setAttribute('data-kv','css');
      st.textContent='.contributors-trigger{display:none!important}.contributors-backdrop{display:none!important}';
      (document.head||document.documentElement).appendChild(st);
    }catch(e){}
  })();
  function noCred(){
    try{
      var t=document.querySelector('.contributors-trigger');
      if(t&&t.style.display!=='none'){t.style.display='none';t.setAttribute('data-kv-off','1');}
      var w=document.querySelector('.contributors-backdrop');
      if(w&&w.style.display!=='none'){w.style.display='none';}
    }catch(e){}
  }
  try{
    var _tk=false;
    var mo=new MutationObserver(function(){
      if(_tk)return;_tk=true;
      requestAnimationFrame(function(){_tk=false;noCred();});
    });
    mo.observe(document.documentElement,{childList:true,subtree:true});
    if(document.readyState!=='loading')noCred();
    else document.addEventListener('DOMContentLoaded',noCred);
  }catch(e){}
})();
</script>
"""

# 首屏弹窗：资源群引导（暗色+金色，与站点主题一致）
AD_DATASOURCE = r"""
<style id="kv-ds-style">
#kv-ds{position:fixed;right:14px;top:10px;z-index:2147483000;display:flex;align-items:center;gap:6px;padding:6px 9px;border-radius:9px;background:rgba(14,16,19,.95);border:1px solid #3a424e;color:#e8e6e3;font:12px/1.3 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif;box-shadow:0 8px 26px rgba(0,0,0,.55)}
#kv-ds.ext{border-color:#5ec27a}
#kv-ds.ext.err{border-color:#d9534f}
#kv-ds .lb{color:#9aa1ab;font-size:11px;letter-spacing:.4px;white-space:nowrap;cursor:pointer;user-select:none}
#kv-ds .lb b{color:#e8e6e3;font-weight:600}
#kv-ds.ext .lb b{color:#7ddc95}
#kv-ds.ext.err .lb b{color:#ef8a86}
#kv-ds .fm{color:#6f7883;font-size:10px;padding:1px 5px;border:1px solid #333a44;border-radius:4px;white-space:nowrap}
#kv-ds .px{display:none;align-items:center;gap:3px;color:#9aa1ab;font-size:11px;cursor:pointer;user-select:none;white-space:nowrap}
#kv-ds.open .px{display:inline-flex}
#kv-ds .px input{margin:0;width:12px;height:12px;accent-color:#e6c260;cursor:pointer}
#kv-ds .gear{color:#8f959e;cursor:pointer;font-size:13px;line-height:1;padding:0 2px;user-select:none}
#kv-ds .gear:hover{color:#e6c260}
#kv-ds input{display:none;width:64px;padding:4px 6px;border-radius:5px;border:1px solid #333a44;background:#0b0d10;color:#e8e6e3;font:11px/1.2 ui-monospace,Consolas,monospace;outline:none}
#kv-ds input.wide{width:150px}
#kv-ds input:focus{border-color:#e6c260}
#kv-ds input::placeholder{color:#5c636d}
#kv-ds button{display:none;padding:4px 9px;border-radius:5px;border:0;background:linear-gradient(180deg,#f0d27a,#d9b455);color:#171717;font-weight:600;font-size:11px;cursor:pointer;white-space:nowrap}
#kv-ds button.gh{border:1px solid #4a5361;background:transparent;color:#c9c6c1;font-weight:400}
#kv-ds button:active{transform:translateY(1px)}
#kv-ds.open input,#kv-ds.open button{display:inline-block}
#kv-ds.cal input,#kv-ds.cal button.calb{display:inline-block}
#kv-ds .dot{width:7px;height:7px;border-radius:50%;background:#5c636d;flex:0 0 auto;cursor:pointer}
#kv-ds.ext .dot{background:#5ec27a;box-shadow:0 0 6px #5ec27a}
#kv-ds.ext.err .dot{background:#d9534f;box-shadow:0 0 6px #d9534f}
#kv-deploy-btn{position:fixed;left:14px;bottom:14px;z-index:2147482000;display:inline-flex;align-items:center;gap:6px;padding:8px 14px;border-radius:10px;background:rgba(14,16,19,.94);border:1px solid rgba(230,194,96,.5);color:#e6c260;font:12.5px/1 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif;text-decoration:none;letter-spacing:.5px;box-shadow:0 8px 24px rgba(0,0,0,.5)}
#kv-deploy-btn:hover{background:rgba(230,194,96,.14);border-color:#e6c260}
#kv-deploy-btn b{font-weight:600}
@media (max-width:720px){#kv-deploy-btn{left:8px;bottom:8px;padding:6px 10px;font-size:11.5px}}
#kv-ds .sep{width:1px;height:16px;background:#333a44;margin:0 1px}
@media (max-width:720px){#kv-ds{right:8px;top:auto;bottom:10px;flex-wrap:wrap;max-width:calc(100vw - 16px)}}
</style>
<script id="kv-ds-js">
(function(){
  var KEY='kv_ds_host',CALKEY='kv_ds_cal',DEF_PORT='8888';
  function norm(v){
    v=String(v||'').trim().replace(/^wss?:\/\//i,'').replace(/^https?:\/\//i,'');
    v=v.split('/')[0].replace(/\s+/g,'');
    if(!/^[A-Za-z0-9.\-]+(:\d{1,5})?$/.test(v))return '';
    if(v.indexOf(':')<0)v=v+':'+DEF_PORT;
    return v;
  }
  var _CHILD=0;
  var _DEF_SRC='';
  var DEFKEY='kv_ds_def';
  try{
    if(_DEF_SRC){
      if((localStorage.getItem(DEFKEY)||'')!==_DEF_SRC){
        localStorage.setItem(KEY,_DEF_SRC);localStorage.setItem(DEFKEY,_DEF_SRC);
      }
    }else if(localStorage.getItem(DEFKEY)){
      localStorage.removeItem(DEFKEY);
    }
  }catch(e){}
  var cur=norm(localStorage.getItem(KEY)||'');
  var PXKEY='kv_ds_proxy';
  var useProxy=(localStorage.getItem(PXKEY)==='1');
  try{
    if(sessionStorage.getItem('kv_ds_auto')==='1'){
      useProxy=true;localStorage.setItem(PXKEY,'1');sessionStorage.removeItem('kv_ds_auto');
    }
  }catch(e){}
  var _pending=null;
  function getCal(){
    try{
      var c=JSON.parse(localStorage.getItem(CALKEY)||'{}');
      return {ox:+c.ox||0,oy:+c.oy||0,s:(+c.s>0?+c.s:1)};
    }catch(e){return {ox:0,oy:0,s:1};}
  }

  /* ---------- 双内核数据自适应 ----------
     新内核：英雄 f[9]=本机阵营(整帧恒定1/2)，塔段 id 1690~1699，坐标基准 340
     老内核：英雄 f[9]=召唤师CD，塔段 x,y,hp,zy,w,h，坐标基准可能不是 340
     统一转成新内核格式；坐标系按整帧最大值自动换算，另叠加手动校准
  ------------------------------------------ */
  function isNewFmt(parts,heroArr){
    var tp=(parts[3]||'').split('==').filter(Boolean)[0];
    if(tp){
      var t=tp.split(',');
      if(t.length>=6&&/^\d+$/.test((t[0]||'').trim())){
        var n=+t[0];
        return n>=1690&&n<=1699;
      }
    }
    var seen=null,ok=heroArr.length>0;
    for(var i=0;i<heroArr.length;i++){
      var f=heroArr[i].split(','),v=(f[9]||'').trim();
      if(v!=='1'&&v!=='2'){ok=false;break;}
      if(seen===null)seen=v;else if(seen!==v){ok=false;break;}
    }
    return ok;
  }
  var _k=1;
  function maxCoord(h,r,t){
    var m=0;
    function p(a,b){a=+a;b=+b;if(isFinite(a)&&a>m)m=a;if(isFinite(b)&&b>m)m=b;}
    for(var i=0;i<h.length;i++){var f=h[i].split(',');p(f[5],f[6]);}
    for(var j=0;j<r.length;j++){var q=r[j].split(',');p(q[3],q[4]);}
    for(var k=0;k<t.length;k++){var w=t[k].split(',');p(w[0],w[1]);}
    return m;
  }
  function cv(v,k,o){ return String(Math.round(((+v)*k+o)*100)/100); }
  // 老内核上报间隔大（几秒一次），坐标是瞬跳；这里做缓动，让头像滑过去
  var _disp={};
  function smooth(id,tx,ty){
    var now=Date.now(),d=_disp[id];
    if(!d||now-d.t>1500){ _disp[id]={x:tx,y:ty,t:now}; return [tx,ty]; }
    var nx=d.x+(tx-d.x)*0.45,ny=d.y+(ty-d.y)*0.45;
    if(Math.abs(tx-nx)<0.25)nx=tx;
    if(Math.abs(ty-ny)<0.25)ny=ty;
    _disp[id]={x:nx,y:ny,t:now};
    return [Math.round(nx*100)/100,Math.round(ny*100)/100];
  }
  function adaptPayload(body){
    if(!body||body.indexOf('---')<0)return body;
    var parts=body.split('---');
    if(parts.length<2)return body;
    var heroArr=parts[0]?parts[0].split('==').filter(Boolean):[];
    if(!heroArr.length)return body;
    if(isNewFmt(parts,heroArr)){_k=1;return body;}

    var resArr=parts[1]?parts[1].split('==').filter(Boolean):[];
    var towerArr=parts[3]?parts[3].split('==').filter(Boolean):[];
    var k=1;   // 不再自动缩放：老内核就是 340 基准，缩放反而导致位置抖动
    var cal=getCal();
    var K=k*cal.s;

    var heroes=[];
    for(var i=0;i<heroArr.length;i++){
      var f=heroArr[i].split(',');
      while(f.length<14)f.push('');
      var _sm=smooth(f[0],(+f[5])*K+cal.ox,(+f[6])*K+cal.oy);
      f[5]=String(_sm[0]);f[6]=String(_sm[1]);
      f[9]='2';
      if(f[10]==='')f[10]='0';
      if(f[13]==='')f[13]='0';
      heroes.push(f.join(','));
    }
    var res=[];
    for(var a=0;a<resArr.length;a++){
      var rr=resArr[a].split(',');
      if(rr.length>=5){rr[3]=cv(rr[3],K,cal.ox);rr[4]=cv(rr[4],K,cal.oy);}
      res.push(rr.join(','));
    }
    var towers=[];
    for(var b=0;b<towerArr.length;b++){
      var t=towerArr[b].split(',');
      if(t.length<6)continue;
      var x=t[0],y=t[1],hp=t[2],zy=t[3];
      if(!isFinite(+x)||!isFinite(+y)||!isFinite(+hp))continue;
      towers.push([1690+b,hp,100,cv(x,K,cal.ox),cv(y,K,cal.oy),String(zy).trim()==='1'?'1':'2','1'].join(','));
    }
    return [heroes.join('=='),res.join('=='),parts[2]||'',towers.join('=='),parts[4]||''].join('---');
  }
  var lastFmt='';
  function adaptMsg(str){
    if(typeof str!=='string')return str;
    var i=str.indexOf('##');
    if(i<0)return str;
    if(str.slice(0,i)!=='gameData')return str;
    var body=str.slice(i+2);
    var nb=adaptPayload(body);
    var fmt=(nb===body)?'yuyan':'s44';
    if(fmt!==lastFmt){
      lastFmt=fmt;
      var el=document.getElementById('kv-ds-fm');
      if(el)el.textContent=fmt==='yuyan'?'新内核':'S44 内核';
    }
    return nb===body?str:str.slice(0,i+2)+nb;
  }
  function installAdapt(){
    var P=window.WebSocket&&window.WebSocket.prototype;
    if(!P)return;
    var d=Object.getOwnPropertyDescriptor(P,'onmessage');
    if(!d||!d.set||d.set.__kv)return;
    var setter=function(fn){
      var self=this;
      if(typeof fn!=='function')return d.set.call(self,fn);
      return d.set.call(self,function(ev){
        try{
          var nd=adaptMsg(ev&&ev.data);
          if(nd!==(ev&&ev.data)){
            ev=new MessageEvent('message',{data:nd,origin:ev.origin,lastEventId:ev.lastEventId,source:ev.source,ports:ev.ports});
          }
        }catch(e){}
        return fn.call(self,ev);
      });
    };
    setter.__kv=1;
    try{Object.defineProperty(P,'onmessage',{configurable:true,enumerable:d.enumerable,get:d.get,set:setter});}catch(e){}
  }
  installAdapt();

  var Native=window.WebSocket;
  if(!Native)return;
  function mark(cls,on){
    var d=document.getElementById('kv-ds');
    if(!d)return;
    on?d.classList.add(cls):d.classList.remove(cls);
  }
  function WSU(u,p){
    var out=String(u),direct=false;
    if(cur){
      try{
        var o=new URL(out,location.href);
        if(o.pathname.indexOf('/ws')===0){
          var ch=cur.split(':')[0];
          var scheme=(o.protocol==='wss:'?'wss://':'ws://');
          if(ch===location.hostname){
            out=scheme+cur+o.pathname+(o.search||'');
          }else if(useProxy){
            out=scheme+location.host+'/proxy-ws?target='+encodeURIComponent(cur)+'&path='+encodeURIComponent(o.pathname);
          }else{
            out=scheme+cur+o.pathname+(o.search||'');
            direct=true;
          }
        }
      }catch(e){}
    }
    var ws=p?new Native(out,p):new Native(out);
    if(cur){
      try{
        ws.addEventListener('open',function(){mark('err',false);});
        ws.addEventListener('error',function(){mark('err',true);});
        ws.addEventListener('close',function(e){if(e&&e.code!==1000)mark('err',true);});
      }catch(e){}
      if(direct){
        _pending=ws;
        setTimeout(function(){
          if(_pending===ws&&ws.readyState===0){
            try{sessionStorage.setItem('kv_ds_auto','1');}catch(e){}
            location.reload();
          }
        },4000);
      }
    }
    return ws;
  }
  WSU.prototype=Native.prototype;
  ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(function(n){try{WSU[n]=Native[n];}catch(e){}});
  try{window.WebSocket=WSU;}catch(e){}
  var oSend=Native.prototype.send;
  // 老内核（S44 jar）订阅只推一次，必须轮询拉取；新内核主动推，多拉一次也无害
  var _pollWs=null,_pollRoom=null,_pollTimer=null;
  function startPoll(ws,room){
    _pollWs=ws;_pollRoom=room;
    if(_pollTimer)return;
    _pollTimer=setInterval(function(){
      if(!_pollWs||_pollWs.readyState!==1||!_pollRoom)return;
      try{oSend.call(_pollWs,'subscribe[==]'+_pollRoom);}catch(e){}
    },100);
  }

  Native.prototype.send=function(d){
    var r=oSend.call(this,d);
    try{
      if(cur){
        var m=String(d);
        if(m==='subscribeHome'||m==='getHome'){try{oSend.call(this,'getHome');}catch(e){}}
        else if(m.indexOf('subscribe[==]')===0){
          var _rm=m.slice(13).trim();
          if(_rm)startPoll(this,_rm);
          try{oSend.call(this,'web'+Date.now()+'[==]'+_rm);}catch(e){}
        }
      }
    }catch(e){}
    return r;
  };
  function build(){
    if(_CHILD)return;   // 客户站不显示数据源面板
    if(document.getElementById('kv-ds'))return;
    var cal=getCal();
    var d=document.createElement('div');
    d.id='kv-ds';
    if(cur)d.className='ext';
    d.innerHTML='<span class="dot"></span><span class="lb">数据源 · <b>'+(cur||'本机')+'</b></span>'
      +'<span class="fm" id="kv-ds-fm">自适应</span>'
      +'<label class="px" title="经本站服务器中转，用于对方校验 Origin 的情况"><input type="checkbox" id="kv-ds-px"'+(useProxy?' checked':'')+'>中转</label>'
      +'<span class="gear" id="kv-ds-gear" title="坐标校准">⚙</span>'
      +'<input id="kv-ds-in" class="wide" spellcheck="false" autocomplete="off" placeholder="IP 默认 8888" value="'+cur+'">'
      +'<button id="kv-ds-go">连接</button>'
      +'<button id="kv-ds-rs" class="gh">本机</button>'
      +'<span class="sep" style="display:none"></span>'
      +'<input id="kv-ds-ox" class="cali" type="number" step="1" title="X 偏移" placeholder="X偏移" value="'+cal.ox+'">'
      +'<input id="kv-ds-oy" class="cali" type="number" step="1" title="Y 偏移" placeholder="Y偏移" value="'+cal.oy+'">'
      +'<input id="kv-ds-s" class="cali" type="number" step="0.01" title="缩放" placeholder="缩放" value="'+cal.s+'">'
      +'<button id="kv-ds-cs" class="calb">应用</button>';
    document.body.appendChild(d);
    try{
      if(!_CHILD&&!document.getElementById('kv-deploy-btn')){
        var _db=document.createElement('a');
        _db.id='kv-deploy-btn';_db.href='/deploy';_db.target='_blank';
        _db.innerHTML='<b>部署同款</b>';
        document.body.appendChild(_db);
      }
    }catch(e){}
    var inp=d.querySelector('#kv-ds-in');
    function toggle(cls){
      d.classList.toggle(cls);
      if(d.classList.contains('open')){inp.focus();inp.select();}
    }
    d.querySelector('.lb').onclick=function(){toggle('open');};
    d.querySelector('.dot').onclick=function(){toggle('open');};
    d.querySelector('#kv-ds-px').onchange=function(e){
      localStorage.setItem(PXKEY,e.target.checked?'1':'0');
      location.reload();
    };
    d.querySelector('#kv-ds-gear').onclick=function(){d.classList.toggle('cal');};
    d.querySelector('#kv-ds-go').onclick=function(){
      var v=norm(inp.value);
      if(!v){alert('格式：IP 或 IP:端口\n例如 101.35.50.29 或 101.35.50.29:8888');return;}
      localStorage.setItem(KEY,v);location.reload();
    };
    d.querySelector('#kv-ds-rs').onclick=function(){
      localStorage.removeItem(KEY);location.reload();
    };
    d.querySelector('#kv-ds-cs').onclick=function(){
      var o={ox:+d.querySelector('#kv-ds-ox').value||0,oy:+d.querySelector('#kv-ds-oy').value||0,s:+d.querySelector('#kv-ds-s').value||1};
      localStorage.setItem(CALKEY,JSON.stringify(o));
      d.classList.remove('cal');
    };
    inp.addEventListener('keydown',function(e){
      if(e.key==='Enter')d.querySelector('#kv-ds-go').click();
      if(e.key==='Escape')d.classList.remove('open');
    });
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',build);else build();
})();
</script>
"""

# 首页右下角「下载 APP」悬浮入口（暗色+金色，与站点主题一致）
APP_DL = r"""
<style id="kv-dl-style">
#kv-dl{position:fixed;right:14px;bottom:14px;z-index:2147482000;display:inline-flex;align-items:center;gap:8px;padding:10px 16px;border-radius:999px;background:linear-gradient(135deg,#f0d27a,#d9b455);color:#1a1408;font:700 13px/1 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif;text-decoration:none;box-shadow:0 10px 26px rgba(0,0,0,.55);cursor:pointer;user-select:none;transition:transform .15s,box-shadow .2s}
#kv-dl:hover{transform:translateY(-1px);box-shadow:0 14px 32px rgba(0,0,0,.6)}
#kv-dl:active{transform:translateY(1px)}
#kv-dl .ic{width:16px;height:16px;flex:0 0 auto}
#kv-dl .tx{display:flex;flex-direction:column;gap:1px;line-height:1}
#kv-dl .tx b{font-size:13px;font-weight:700}
#kv-dl .tx i{font-style:normal;font-size:10px;font-weight:500;color:rgba(26,20,8,.62);letter-spacing:.04em}
#kv-dl::before{content:"";position:absolute;inset:0;border-radius:999px;animation:kv-dl-pulse 2.6s ease-out infinite;pointer-events:none}
@keyframes kv-dl-pulse{0%{box-shadow:0 0 0 0 rgba(230,194,96,.5)}70%{box-shadow:0 0 0 13px rgba(230,194,96,0)}100%{box-shadow:0 0 0 0 rgba(230,194,96,0)}}
@media (max-width:720px){#kv-dl{right:auto;left:8px;bottom:8px;padding:8px 12px;gap:6px}#kv-dl .tx i{display:none}}
</style>
<a id="kv-dl" href="/app.apk" download="king-share.apk" title="安卓客户端 · 点击下载安装">
  <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v10.5"/><path d="M7.5 9.5L12 14l4.5-4.5"/><path d="M5 19.5h14"/></svg>
  <span class="tx"><b>下载 APP</b><i>安卓客户端</i></span>
</a>
"""

AD_POPUP = r"""
<style id="kv-pop-style">
#kv-pop-mask{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;justify-content:center;padding:20px;background:rgba(4,6,8,.74);-webkit-backdrop-filter:blur(4px);backdrop-filter:blur(4px);opacity:0;transition:opacity .22s ease;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif}
#kv-pop-mask.kv-in{opacity:1}
#kv-pop-card{position:relative;width:min(430px,92vw);border-radius:16px;padding:26px 24px 20px;box-sizing:border-box;color:#e9edf2;background:linear-gradient(158deg,#161a20 0%,#0e1013 62%,#0b0d10 100%);border:1px solid rgba(230,194,96,.34);box-shadow:0 26px 64px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.04);transform:translateY(12px) scale(.97);transition:transform .26s cubic-bezier(.2,.8,.3,1);overflow:hidden}
#kv-pop-mask.kv-in #kv-pop-card{transform:translateY(0) scale(1)}
#kv-pop-card::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;background:linear-gradient(90deg,rgba(230,194,96,0),#e6c260,rgba(230,194,96,0))}
#kv-pop-card::after{content:"";position:absolute;right:-70px;top:-70px;width:190px;height:190px;border-radius:50%;background:radial-gradient(circle,rgba(230,194,96,.14),rgba(230,194,96,0) 70%);pointer-events:none}
#kv-pop-close{position:absolute;right:12px;top:10px;width:26px;height:26px;line-height:24px;text-align:center;border-radius:50%;color:#7d858f;background:transparent;border:0;cursor:pointer;font-size:17px;transition:color .18s,background .18s}
#kv-pop-close:hover{color:#e6c260;background:rgba(230,194,96,.1)}
#kv-pop-eyebrow{font-size:10px;letter-spacing:.22em;color:#8b7434;text-transform:uppercase;margin-bottom:10px}
#kv-pop-title{font-size:21px;font-weight:700;letter-spacing:.02em;margin:0 0 8px;color:#f3f5f7}
#kv-pop-title em{font-style:normal;color:#e6c260}
#kv-pop-desc{font-size:13px;line-height:1.7;color:#9aa3ad;margin:0 0 18px}
#kv-pop-group{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-radius:10px;background:rgba(230,194,96,.06);border:1px solid rgba(230,194,96,.2);cursor:pointer;transition:border-color .18s,background .18s}
#kv-pop-group:hover{border-color:rgba(230,194,96,.45);background:rgba(230,194,96,.1)}
#kv-pop-group .kv-l{font-size:11px;color:#8b939d;letter-spacing:.08em}
#kv-pop-group .kv-n{font-size:18px;font-weight:700;color:#e6c260;letter-spacing:.06em;font-family:Consolas,Menlo,monospace}
#kv-pop-group .kv-c{font-size:10px;color:#7d858f;border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:3px 7px;white-space:nowrap}
#kv-pop-actions{display:flex;gap:10px;margin-top:18px}
#kv-pop-actions button{flex:1;height:40px;border-radius:9px;font-size:13.5px;font-weight:600;cursor:pointer;transition:transform .15s,box-shadow .18s,background .18s,color .18s;font-family:inherit}
#kv-pop-cancel{background:transparent;color:#98a1ab;border:1px solid rgba(255,255,255,.14)}
#kv-pop-cancel:hover{color:#dfe4ea;border-color:rgba(255,255,255,.3)}
#kv-pop-join{background:linear-gradient(135deg,#f0d38a,#d9b45c);color:#241c07;border:1px solid rgba(230,194,96,.6);box-shadow:0 6px 18px rgba(217,180,92,.22)}
#kv-pop-join:hover{transform:translateY(-1px);box-shadow:0 9px 22px rgba(217,180,92,.32)}
#kv-pop-tip{font-size:11px;color:#6f7883;margin:12px 0 0;text-align:center}
#kv-pop-toast{position:fixed;left:50%;bottom:40px;transform:translateX(-50%) translateY(8px);background:rgba(20,23,28,.96);color:#e6c260;border:1px solid rgba(230,194,96,.35);padding:8px 16px;border-radius:20px;font-size:12px;opacity:0;transition:opacity .2s,transform .2s;z-index:2147483001;pointer-events:none;font-family:"Microsoft YaHei",system-ui,sans-serif}
#kv-pop-toast.kv-in{opacity:1;transform:translateX(-50%) translateY(0)}
@media (max-width:420px){#kv-pop-card{padding:22px 18px 16px}#kv-pop-title{font-size:18px}#kv-pop-actions button{height:38px;font-size:13px}}
</style>
<script>
(function(){
  function build(){
    if(document.getElementById('kv-pop-mask'))return;
    var mask=document.createElement('div');mask.id='kv-pop-mask';
    mask.innerHTML=''
      +'<div id="kv-pop-card">'
      +'<button id="kv-pop-close" aria-label="关闭">&#215;</button>'
      +'<div id="kv-pop-eyebrow">@@EYEBROW@@</div>'
      +'<h3 id="kv-pop-title">@@TITLE@@</h3>'
      +'<p id="kv-pop-desc">@@DESC@@</p>'
      +'<div id="kv-pop-group" title="点击复制群号"><div><div class="kv-l">QQ 交流群</div><div class="kv-n">@@GROUP@@</div></div><span class="kv-c">点击复制</span></div>'
      +'<div id="kv-pop-actions"><button id="kv-pop-cancel">取消</button><button id="kv-pop-join">去加群</button></div>'
      +'<p id="kv-pop-tip">@@TIP@@</p>'
      +'</div>';
    document.body.appendChild(mask);
    requestAnimationFrame(function(){mask.classList.add('kv-in');});
    function toast(t){
      var old=document.getElementById('kv-pop-toast');if(old)old.remove();
      var el=document.createElement('div');el.id='kv-pop-toast';el.textContent=t;
      document.body.appendChild(el);
      requestAnimationFrame(function(){el.classList.add('kv-in');});
      setTimeout(function(){el.classList.remove('kv-in');setTimeout(function(){el.remove();},250);},1800);
    }
    function copy(txt){
      try{
        if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt);return true;}
      }catch(e){}
      try{var ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';
        document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();return true;}catch(e){return false;}
    }
    function close(){
      mask.classList.remove('kv-in');
      setTimeout(function(){if(mask&&mask.parentNode)mask.parentNode.removeChild(mask);},230);
    }
    mask.querySelector('#kv-pop-close').onclick=close;
    mask.querySelector('#kv-pop-cancel').onclick=close;
    mask.querySelector('#kv-pop-group').onclick=function(){
      copy('@@GROUP@@');toast('群号 @@GROUP@@ 已复制');
    };
    mask.querySelector('#kv-pop-join').onclick=function(){
      copy('@@GROUP@@');
      toast('群号 @@GROUP@@ 已复制，打开 QQ 搜索加入');
      setTimeout(close,900);
    };
    mask.addEventListener('click',function(e){if(e.target===mask)close();});
    document.addEventListener('keydown',function(e){if(e.key==='Escape')close();});
  }
  var KV_REPEAT=@@REPEAT_MS@@;
  function boot(){
    try{
      var k='kv_pop_day',last=parseInt(localStorage.getItem(k)||'0',10);
      if(KV_REPEAT>0&&last&&(Date.now()-last)<KV_REPEAT){return;}
      localStorage.setItem(k,String(Date.now()));
    }catch(e){}
    setTimeout(build,700);
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',boot);}
  else{boot();}
})();
</script>
"""


def popup_html():
    """按后台配置渲染前台资源群弹窗；popup_on 关闭时返回空串。"""
    if not CONFIG.get("popup_on", True):
        return ""
    try:
        days = float(CONFIG.get("popup_repeat_days") or 0)
    except Exception:
        days = 1.0
    repeat_ms = int(days * 86400000) if days > 0 else 0
    group = re.sub(r"[^0-9]", "", str(CONFIG.get("popup_group") or "")) or "862938931"
    out = AD_POPUP
    for token, val in (
        ("@@EYEBROW@@", esc(CONFIG.get("popup_eyebrow") or "")),
        ("@@TITLE@@", str(CONFIG.get("popup_title") or "")),
        ("@@DESC@@", esc(CONFIG.get("popup_desc") or "")),
        ("@@TIP@@", esc(CONFIG.get("popup_tip") or "")),
        ("@@GROUP@@", group),
        ("@@REPEAT_MS@@", str(repeat_ms)),
    ):
        out = out.replace(token, val)
    return out


def http_response(writer, code, body, ctype="text/plain; charset=utf-8", extra="", cache="no-store"):
    if isinstance(body, str):
        body = body.encode()
    head = (
        "HTTP/1.1 %d %s\r\n"
        "Server: nginx\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %d\r\n"
        "Cache-Control: %s\r\n"
        "%s"
        "Connection: close\r\n\r\n"
    ) % (code, {"200": "OK", "302": "Found", "403": "Forbidden",
                "404": "Not Found"}.get(str(code), "OK"),
         ctype, len(body), cache, extra)
    payload = head.encode() + body
    STATS["down"] += len(payload)
    writer.write(payload)


# ---------------------------------------------------------------- 管理后台
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


ADMIN_STYLE = """<style>
:root{--gold:#e6c260;--gold2:#d9b45c;--bg:#0a0c0f;--card:#14181d;--line:rgba(255,255,255,.08);
--txt:#e9edf2;--dim:#9aa3ad;--ok:#5fd39a;--bad:#ff7a7a}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--txt);font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
background:radial-gradient(1100px 520px at 50% -12%,rgba(230,194,96,.08),transparent 62%),var(--bg)}
a{color:var(--gold);text-decoration:none}
.wrap{max-width:1040px;margin:0 auto;padding:26px 20px 60px}
.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:10px}
.brand i{width:26px;height:26px;border-radius:8px;display:inline-block;
background:linear-gradient(135deg,#f0d38a,#c9a144);box-shadow:0 0 18px rgba(230,194,96,.35)}
.brand b{font-size:16px;letter-spacing:.04em}
.brand span{font-size:11px;color:#7d858f;letter-spacing:.18em}
.nav{display:flex;gap:14px;font-size:12.5px;color:var(--dim)}
.card{background:linear-gradient(160deg,rgba(255,255,255,.03),rgba(255,255,255,.01));border:1px solid var(--line);
border-radius:14px;padding:20px 22px;margin-bottom:16px;position:relative;overflow:hidden}
.card::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;
background:linear-gradient(90deg,transparent,rgba(230,194,96,.55),transparent)}
.card h3{margin:0 0 6px;font-size:15px;letter-spacing:.02em;display:flex;align-items:center;gap:8px}
.card h3 em{font-style:normal;font-size:11px;color:#7d858f;font-weight:400;letter-spacing:.06em}
.card p.tip{margin:0 0 14px;font-size:12.5px;color:var(--dim);line-height:1.75}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.stat{background:rgba(255,255,255,.02);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.stat .k{font-size:11px;color:#7d858f;letter-spacing:.12em;margin-bottom:6px}
.stat .v{font-size:20px;font-weight:700;color:var(--gold);font-family:Consolas,Menlo,monospace}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:38px;padding:0 18px;border-radius:9px;
font-size:13px;font-weight:600;cursor:pointer;border:1px solid rgba(230,194,96,.55);color:#241c07;
background:linear-gradient(135deg,#f0d38a,#d9b45c);box-shadow:0 6px 18px rgba(217,180,92,.18);transition:.18s}
.btn:hover{transform:translateY(-1px);box-shadow:0 10px 24px rgba(217,180,92,.28)}
.btn.ghost{background:transparent;color:var(--dim);border:1px solid var(--line);box-shadow:none}
.btn.ghost:hover{color:var(--txt);border-color:rgba(255,255,255,.28);transform:none;box-shadow:none}
.btn.mini{height:26px;padding:0 10px;font-size:11.5px;font-weight:500;border-radius:7px;box-shadow:none}
form.add{display:grid;grid-template-columns:1.3fr 1fr 1fr auto;gap:10px;margin:4px 0 16px}
input{height:36px;width:100%;padding:0 12px;border-radius:8px;background:rgba(255,255,255,.03);color:var(--txt);
border:1px solid var(--line);font-size:12.5px;font-family:inherit;outline:none;transition:.16s}
input:focus{border-color:rgba(230,194,96,.5);background:rgba(230,194,96,.05)}
input::placeholder{color:#5d656e}
textarea{grid-column:1/-1;width:100%;padding:10px 12px;border-radius:8px;background:rgba(255,255,255,.03);
color:var(--txt);border:1px solid var(--line);font-size:12.5px;font-family:Consolas,Menlo,monospace;
line-height:1.6;outline:none;resize:vertical;transition:.16s}
textarea:focus{border-color:rgba(230,194,96,.5);background:rgba(230,194,96,.05)}
label.chk{display:inline-flex;align-items:center;gap:7px;height:36px;font-size:12.5px;color:var(--dim);cursor:pointer}
label.chk input{width:15px;height:15px;accent-color:#e6c260;cursor:pointer}
select.sel{height:36px;width:100%;padding:0 10px;border-radius:8px;background:rgba(255,255,255,.03);
color:var(--txt);border:1px solid var(--line);font-size:12.5px;font-family:inherit;outline:none;cursor:pointer}
select.sel option{background:#14171c;color:#e8eaed}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;font-weight:500;color:#7d858f;font-size:11px;letter-spacing:.08em;padding:8px 10px;
border-bottom:1px solid var(--line)}
td{padding:11px 10px;border-bottom:1px solid rgba(255,255,255,.05);vertical-align:middle}
tr:last-child td{border-bottom:0}
code.k{font-family:Consolas,Menlo,monospace;color:var(--gold);font-size:12.5px;letter-spacing:.04em}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;border:1px solid}
.tag.on{color:var(--ok);border-color:rgba(95,211,154,.35);background:rgba(95,211,154,.08)}
.tag.off{color:#8b939d;border-color:rgba(255,255,255,.14);background:rgba(255,255,255,.03)}
.tag.exp{color:var(--bad);border-color:rgba(255,122,122,.35);background:rgba(255,122,122,.08)}
.muted{color:#6f7883}
.msg{padding:10px 14px;border-radius:9px;font-size:12.5px;margin-bottom:14px;border:1px solid}
.msg.ok{color:var(--ok);border-color:rgba(95,211,154,.3);background:rgba(95,211,154,.07)}
.msg.err{color:var(--bad);border-color:rgba(255,122,122,.3);background:rgba(255,122,122,.07)}
.login{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.login .card{width:min(380px,92vw);padding:30px 26px 24px;margin:0}
.login h2{margin:0 0 4px;font-size:19px}
.login .sub{font-size:12px;color:#7d858f;margin:0 0 22px;letter-spacing:.08em}
.login label{display:block;font-size:11.5px;color:#8b939d;margin:14px 0 6px;letter-spacing:.06em}
.login .btn{width:100%;margin-top:20px;height:40px}
.empty{padding:26px 8px;text-align:center;color:#6f7883;font-size:12.5px}
@media(max-width:760px){.stats{grid-template-columns:repeat(2,1fr)}form.add{grid-template-columns:1fr}
.tablewrap{overflow-x:auto}}
</style>"""


# ---------------------------------------------------------------- 一键部署
NODE_FILE = os.path.join(DATA_DIR, "nodes.json")
_NODES = None


def load_nodes():
    global _NODES
    if _NODES is not None:
        return _NODES
    try:
        with open(NODE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, list):
            d = []
    except Exception:
        d = []
    _NODES = d
    return _NODES


def save_nodes(items=None):
    global _NODES
    if items is not None:
        _NODES = items
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = NODE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump((_NODES or [])[-200:], f, ensure_ascii=False, indent=1)
        os.replace(tmp, NODE_FILE)
        return True
    except Exception as e:
        log("写部署记录失败:", repr(e))
        return False


def get_node(nid):
    for it in load_nodes():
        if it.get("id") == nid:
            return it
    return None


def _say(rec, msg):
    lines = rec.setdefault("log_list", [])
    lines.append(time.strftime("%H:%M:%S ") + str(msg))
    rec["log"] = "\n".join(lines[-300:])
    rec["updated"] = int(time.time())
    save_nodes()


def _ssh_connect(host, port, user, pwd, timeout=12):
    import paramiko
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(str(host).strip(), port=int(port), username=str(user).strip(),
              password=str(pwd), timeout=timeout, banner_timeout=25,
              auth_timeout=25, look_for_keys=False, allow_agent=False)
    return c


def _remote_port_free(cli, port):
    """远端端口是否空闲。"""
    try:
        _r, _o, _e = _ssh_run(
            cli, "ss -lnt 2>/dev/null | awk '{print $4}' | grep -c ':%d$'" % int(port), timeout=20)
        return int((_o or "0").strip().splitlines()[-1] or 0) == 0
    except Exception:
        return True


def _port_state(cli, port):
    """返回 (是否被占, 是否是我们自己的 relay 实例)。
    自己的旧实例可以直接替换：deploy_node 会先 stop 掉旧 unit。"""
    try:
        _r, _o, _e = _ssh_run(
            cli, "ss -lntp 2>/dev/null | grep ':%d ' | head -1" % int(port), timeout=20)
        line = (_o or "").strip()
        if not line:
            return False, False
        if "king-relay-lite" in line:
            return True, True
        _r2, _o2, _e2 = _ssh_run(
            cli, "for p in $(ss -lntp 2>/dev/null | grep ':%d ' | grep -oE 'pid=[0-9]+' | cut -d= -f2); "
                 "do ps -p $p -o args= 2>/dev/null; done" % int(port), timeout=20)
        if "king-relay-lite" in (_o2 or ""):
            return True, True
        return True, False
    except Exception:
        return False, False


def _ssh_run(cli, cmd, timeout=120, stdin_data=None):
    si, so, se = cli.exec_command(cmd, timeout=timeout)
    if stdin_data:
        # 大包必须异步写，否则 stdout 未读时可能死锁
        import threading as _th

        def _feed():
            try:
                si.write(stdin_data)
                si.flush()
                si.channel.shutdown_write()
            except Exception:
                pass

        _th.Thread(target=_feed, daemon=True).start()
    out = so.read().decode("utf-8", "ignore")
    err = se.read().decode("utf-8", "ignore")
    try:
        rc = so.channel.recv_exit_status()
    except Exception:
        rc = -1
    return rc, out, err


def _q(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def node_cfg(rec):
    """子站 config.json 内容（同时是「远程推配置」的载荷来源）。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(rec.get("cfg") or {})
    cfg["site_name"] = rec.get("site_name") or cfg.get("site_name")
    cfg["admin_pass"] = rec.get("admin_pass") or cfg.get("admin_pass")
    if LICENSE_OFF:
        # 免授权：子站不写授权服务器，只保留上游镜像地址（那是数据源，不是授权）
        cfg["license_server"] = ""
        cfg["license_code"] = ""
    else:
        cfg["license_server"] = (str(CONFIG.get("public_url") or "").strip()
                                 or "http://%s:%d/" % (public_host(), WEB_PORT))
        cfg["license_code"] = rec.get("license") or ""
    cfg["child_mode"] = True   # 客户站不带后台
    # 子站不采集数据，房间列表由母站镜像过来；母站数据 WS 端口固定 8888
    cfg["upstream"] = "%s:%d" % (public_host(), WS_PORT)
    cfg["upstream_on"] = True
    cfg.pop("license_verify_off", None)
    return cfg


def build_deploy_tar(rec):
    import io as _io
    import tarfile as _tar
    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w:gz") as t:
        data = open(os.path.abspath(__file__), "rb").read()
        ti = _tar.TarInfo("king-relay-lite.py")
        ti.size = len(data)
        ti.mode = 0o644
        t.addfile(ti, _io.BytesIO(data))
        root = os.path.join(BASE_DIR, "dist")
        if os.path.isdir(root):
            for base, _d, files in os.walk(root):
                for fn in files:
                    full = os.path.join(base, fn)
                    rel = os.path.relpath(full, root).replace("\\", "/")
                    if rel.startswith("source/"):
                        continue
                    t.add(full, "dist/" + rel)
        blob = json.dumps(node_cfg(rec), ensure_ascii=False, indent=2).encode("utf-8")
        ti = _tar.TarInfo("config.json")
        ti.size = len(blob)
        ti.mode = 0o600
        t.addfile(ti, _io.BytesIO(blob))
    return buf.getvalue()


def _unit_text(rec):
    d = str(rec.get("dir") or "").rstrip("/")
    return (
        "[Unit]\n"
        "Description=King Vision Relay %s\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "WorkingDirectory=%s\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Environment=WS_PORT=%d\n"
        "Environment=TCP_PORT=%d\n"
        "Environment=WEB_PORT=%d\n"
        "Environment=STATIC_DIR=%s/dist\n"
        "Environment=ROOM_TTL=60\n"
        "ExecStart=/usr/bin/python3 %s/king-relay-lite.py\n"
        "Restart=always\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
        % (rec.get("id"), d, int(rec.get("ws_port") or 8888),
           int(rec.get("tcp_port") or 9999),
           int(rec.get("web_port") or WEB_PORT), d, d))


def deploy_local(nid):
    """后台线程：不走 SSH，直接在本机复制一套并起独立 systemd 实例。"""
    import shutil
    import subprocess
    rec = get_node(nid)
    if not rec:
        return
    rec["status"] = "running"
    rec["log_list"] = []
    rec["log"] = ""
    _say(rec, "开始本机部署 → %s" % (rec.get("dir") or ""))
    try:
        d = str(rec.get("dir") or "").rstrip("/")
        if not d or d in ("/", "/www", "/www/wwwroot"):
            raise RuntimeError("安装目录不合法：%s" % d)
        # 数据通道固定 8888/9999，网页固定 WEB_PORT；本机已被占就报错，不自动挪
        wp, tp, wep = 8888, 9999, WEB_PORT
        for _p in (wp, wep):
            if _in_use(_p):
                raise RuntimeError("本机 %d 已被占用，无法再开新实例" % _p)
        rec["ws_port"], rec["tcp_port"], rec["web_port"] = wp, tp, wep
        _say(rec, "端口固定 ws=%d tcp=%d，网页 %d" % (wp, tp, wep))

        os.makedirs(os.path.join(d, "dist"), exist_ok=True)
        shutil.copy2(os.path.abspath(__file__), os.path.join(d, "king-relay-lite.py"))
        src = os.path.join(BASE_DIR, "dist")
        cnt = 0
        if os.path.isdir(src):
            for base, _dirs, files in os.walk(src):
                for fn in files:
                    full = os.path.join(base, fn)
                    rel = os.path.relpath(full, src).replace("\\", "/")
                    if rel.startswith("source/"):
                        continue
                    dst = os.path.join(d, "dist", rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(full, dst)
                    cnt += 1
        _say(rec, "文件已复制 %d 个 → %s" % (cnt, d))

        cfg_path = os.path.join(d, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(node_cfg(rec), fh, ensure_ascii=False, indent=2)
        try:
            os.chmod(cfg_path, 0o600)
        except Exception:
            pass

        unit = str(rec.get("unit"))
        with open("/etc/systemd/system/%s.service" % unit, "w", encoding="utf-8") as fh:
            fh.write(_unit_text(rec))
        _say(rec, "已写入 /etc/systemd/system/%s.service" % unit)

        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "enable", unit], capture_output=True)
        subprocess.run(["systemctl", "restart", unit], capture_output=True)
        time.sleep(2)
        act = subprocess.run(["systemctl", "is-active", unit],
                             capture_output=True, text=True).stdout.strip() or "unknown"
        code = ""
        for _ in range(8):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/" % wep, timeout=3) as r:
                    code = str(getattr(r, "status", 0) or r.getcode() or "")
            except Exception:
                code = ""
            if code == "200":
                break
            time.sleep(1)
        rec["active"] = act
        rec["http_code"] = code
        rec["status"] = "ok" if (act == "active" and code == "200") else "warn"
        _say(rec, "systemd=%s  http=%s" % (act, code))
        _say(rec, "站点 http://%s:%d/  ·  后台 http://%s:%d/admin  密码 %s"
             % (rec.get("host"), wep, rec.get("host"), wep, rec.get("admin_pass")))
        _say(rec, "授权码 %s（到期 %s）" % (rec.get("license"), rec.get("expire") or "永久"))
        if rec["status"] == "warn":
            try:
                jl = subprocess.run(["journalctl", "-u", unit, "-n", "30", "--no-pager"],
                                    capture_output=True, text=True).stdout
                if jl.strip():
                    _say(rec, "--- journalctl ---")
                    for ln in jl.strip().splitlines()[-30:]:
                        _say(rec, ln[:300])
            except Exception:
                pass
    except Exception as e:
        rec["status"] = "fail"
        _say(rec, "失败：%s" % repr(e)[:300])
    finally:
        _say(rec, "任务结束，状态 %s" % rec.get("status"))


def _in_use(port):
    """判断本机端口是否被占用。"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            if s:
                s.close()
        except Exception:
            pass


def deploy_node(nid):
    """后台线程：SSH 到目标机，装 python3、上包、写 systemd、起服务、验端口。"""
    import base64 as _b64
    rec = get_node(nid)
    if not rec:
        return
    rec["status"] = "running"
    rec["log_list"] = []
    rec["log"] = ""
    _say(rec, "开始部署 → %s@%s:%s" % (rec.get("ssh_user"), rec.get("host"), rec.get("ssh_port")))
    cli = None
    try:
        cli = _ssh_connect(rec["host"], rec.get("ssh_port") or 22,
                           rec.get("ssh_user") or "root", rec.get("ssh_pass") or "")
        _say(rec, "SSH 连接成功")

        rc, out, _e = _ssh_run(cli, "command -v python3 || echo __NOPY__")
        if "__NOPY__" in out:
            _say(rec, "目标机没有 python3，尝试安装…")
            _ssh_run(cli, "(apt-get update -qq && apt-get install -y python3) >/dev/null 2>&1 "
                          "|| (yum install -y python3) >/dev/null 2>&1", timeout=420)
            rc, out, _e = _ssh_run(cli, "command -v python3 || echo __NOPY__")
            if "__NOPY__" in out:
                raise RuntimeError("目标机缺少 python3 且自动安装失败")
        _say(rec, "python3: " + (out.strip().splitlines() or [""])[0])
        # 同一台服务器重复部署：先停掉旧实例，把端口让出来
        try:
            _olds = [x for x in load_nodes()
                     if x.get("host") == rec.get("host") and x.get("id") != rec.get("id")
                     and x.get("unit")]
            for _o in _olds:
                _ssh_run(cli, "systemctl stop %s >/dev/null 2>&1; "
                              "systemctl disable %s >/dev/null 2>&1; "
                              "rm -f /etc/systemd/system/%s.service"
                         % (_o["unit"], _o["unit"], _o["unit"]), timeout=60)
                _say(rec, "已清理旧实例 %s（释放端口）" % _o["unit"])
                _o["status"] = "replaced"
            if _olds:
                save_nodes()
        except Exception as _ex:
            _say(rec, "清理旧实例时出错（忽略）：%s" % repr(_ex)[:80])

        # 再按目标机上的实际 unit 文件扫一遍：记录被删了也不会漏
        try:
            _r2, _o2, _e2 = _ssh_run(
                cli, "ls /etc/systemd/system/kv-*.service 2>/dev/null | sed 's#.*/##;s#.service##'")
            _units = [u.strip() for u in (_o2 or "").splitlines() if u.strip().startswith("kv-")]
            _stopped = []
            for _u in _units:
                if _u == rec.get("unit"):
                    continue
                _ssh_run(cli, "systemctl stop %s >/dev/null 2>&1; "
                              "systemctl disable %s >/dev/null 2>&1; "
                              "rm -f /etc/systemd/system/%s.service" % (_u, _u, _u), timeout=60)
                _stopped.append(_u)
            if _stopped:
                _ssh_run(cli, "systemctl daemon-reload", timeout=30)
                _say(rec, "已停掉同机旧实例：%s" % ", ".join(_stopped))
                time.sleep(1.5)
        except Exception as _ex2:
            _say(rec, "扫描旧实例出错（忽略）：%s" % repr(_ex2)[:80])

        # 端口固定：数据通道 8888/9999（和 APP 客户端锁死），网页独立 8080
        rec["ws_port"] = WS_PORT if WS_PORT == 8888 else 8888
        rec["tcp_port"] = 9999
        rec["web_port"] = WEB_PORT

        def _port_free(p):
            try:
                _r, _o, _e = _ssh_run(
                    cli, "ss -lnt 2>/dev/null | awk '{print $4}' | grep -c ':%d$'" % p)
                return int((_o or "0").strip().splitlines()[-1] or 0) == 0
            except Exception:
                return True
        if not _port_free(8888):
            raise RuntimeError("目标机 8888 端口已被占用，请先停掉占用进程再部署")
        if not _port_free(WEB_PORT):
            raise RuntimeError("目标机 %d 网页端口已被占用，请先停掉占用进程再部署" % WEB_PORT)
        _say(rec, "端口固定 ws=8888 tcp=9999（与 APP 客户端一致），网页独立 %d" % WEB_PORT)

        d = str(rec.get("dir") or "").rstrip("/")
        _ssh_run(cli, "mkdir -p %s/dist" % _q(d))
        tarbytes = build_deploy_tar(rec)
        rc, out, err = _ssh_run(cli, "tar -xz -C %s" % _q(d), stdin_data=tarbytes, timeout=300)
        if rc != 0:
            raise RuntimeError("上传解包失败：%s" % ((err or out)[:200].strip()))
        _say(rec, "文件已就位（%.0f KB → %s）" % (len(tarbytes) / 1024.0, d))
        _ssh_run(cli, "chmod 600 %s/config.json 2>/dev/null || true" % _q(d))

        unit = str(rec.get("unit"))
        ub = _b64.b64encode(_unit_text(rec).encode()).decode()
        rc, out, err = _ssh_run(cli, "echo %s | base64 -d > /etc/systemd/system/%s.service"
                                     % (ub, unit))
        if rc != 0:
            raise RuntimeError("写 systemd 单元失败：%s" % (err or out)[:200])
        _say(rec, "已写入 /etc/systemd/system/%s.service" % unit)

        _ssh_run(cli, "systemctl daemon-reload && systemctl enable %s >/dev/null 2>&1; "
                      "systemctl restart %s" % (unit, unit), timeout=120)
        time.sleep(2)
        _rc, out, _e = _ssh_run(cli, "systemctl is-active %s" % unit)
        active = out.strip() or "unknown"

        code = ""
        wp = int(rec.get("web_port") or WEB_PORT)
        for _ in range(8):
            _rc, o2, _e = _ssh_run(
                cli, "curl -s -o /dev/null -m 3 -w '%%{http_code}' http://127.0.0.1:%d/" % wp)
            code = (o2 or "").strip()
            if code == "200":
                break
            time.sleep(1)
        _say(rec, "systemd=%s  http=%s" % (active, code))

        _ssh_run(cli, "command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi active "
                      "&& ufw allow %d/tcp >/dev/null 2>&1 && ufw allow %d/tcp >/dev/null 2>&1 "
                      "&& ufw allow %d/tcp >/dev/null 2>&1 || true"
                 % (wp, int(rec.get("ws_port") or 8888), int(rec.get("tcp_port") or 9999)))
        _ssh_run(cli, "command -v firewall-cmd >/dev/null 2>&1 && "
                      "firewall-cmd --add-port=%d/tcp --permanent >/dev/null 2>&1 "
                      "&& firewall-cmd --add-port=%d/tcp --permanent >/dev/null 2>&1 "
                      "&& firewall-cmd --add-port=%d/tcp --permanent >/dev/null 2>&1 "
                      "&& firewall-cmd --reload >/dev/null 2>&1 || true"
                 % (wp, int(rec.get("ws_port") or 8888), int(rec.get("tcp_port") or 9999)))

        _ssh_run(cli, "python3 -c 'import paramiko' 2>/dev/null || "
                      "python3 -m pip install --break-system-packages -q paramiko "
                      ">/dev/null 2>&1 || true", timeout=180)

        rec["active"] = active
        rec["http_code"] = code
        rec["status"] = "ok" if (active == "active" and code == "200") else "warn"
        _say(rec, "站点地址 http://%s:%d/  ·  后台 http://%s:%d/admin  密码 %s"
             % (rec.get("host"), wp, rec.get("host"), wp, rec.get("admin_pass")))
        _say(rec, "授权码 %s（已写入子站 config.json，到期 %s）"
             % (rec.get("license"), rec.get("expire") or "永久"))
        _say(rec, "云服务器记得在安全组放行 %d/tcp（网页）、%d/tcp（数据 WS）、%d/tcp（客户端上报）"
             % (wp, int(rec.get("ws_port") or 8888), int(rec.get("tcp_port") or 9999)))
        if rec["status"] == "warn":
            try:
                _r3, _pv, _e3 = _ssh_run(cli, "python3 -V 2>&1")
                _say(rec, "python: " + ((_pv or "").strip().splitlines() or [""])[0])
            except Exception:
                pass
            try:
                _r4, _jl, _e4 = _ssh_run(
                    cli, "journalctl -u %s -n 40 --no-pager 2>&1 | tail -40" % unit, timeout=60)
                _txt = (_jl or "").strip()
                if _txt:
                    _say(rec, "--- journalctl ---")
                    for _ln in _txt.splitlines()[-40:]:
                        _say(rec, _ln[:300])
            except Exception as _ex:
                _say(rec, "journalctl 读取失败：%s" % repr(_ex)[:120])
            _say(rec, "提示：服务没到 active/200，多为目标机 python3 版本过低（需 3.6+）或端口占用")
    except Exception as e:
        rec["status"] = "fail"
        _say(rec, "失败：%s" % repr(e)[:300])
    finally:
        try:
            if cli:
                cli.close()
        except Exception:
            pass
        if rec.get("status") == "ok" and rec.get("deploy_code"):
            mark_deploy_code_used(rec.get("deploy_code"), rec.get("host"))
            _say(rec, "部署卡密 %s 已标记为已用" % rec.get("deploy_code"))
        _say(rec, "任务结束，状态 %s" % rec.get("status"))


def _sync_popup_cfg(rec):
    """把母站当前的弹窗/公告配置同步进节点 cfg。"""
    cf = rec.setdefault("cfg", {})
    for k in ("popup_on", "popup_repeat_days", "popup_eyebrow",
              "popup_title", "popup_desc", "popup_tip", "popup_group"):
        if k in CONFIG:
            cf[k] = CONFIG[k]
    return cf


def node_action(rec, action, extra=None):
    """对已部署节点做远程动作。返回提示文本。"""
    extra = extra or {}
    cli = None
    try:
        cli = _ssh_connect(rec["host"], rec.get("ssh_port") or 22,
                           rec.get("ssh_user") or "root", rec.get("ssh_pass") or "")
        unit = str(rec.get("unit"))
        d = str(rec.get("dir") or "").rstrip("/")
        if action == "restart":
            _ssh_run(cli, "systemctl restart %s" % unit, timeout=90)
            time.sleep(1)
            _rc, out, _e = _ssh_run(cli, "systemctl is-active %s" % unit)
            return "已重启，状态 " + (out.strip() or "unknown")
        if action == "stop":
            _ssh_run(cli, "systemctl stop %s; systemctl disable %s >/dev/null 2>&1"
                     % (unit, unit), timeout=90)
            return "已停止并取消开机启动"
        if action == "start":
            _ssh_run(cli, "systemctl enable %s >/dev/null 2>&1; systemctl restart %s"
                     % (unit, unit), timeout=90)
            return "已启动"
        if action == "push":
            import base64 as _b64
            blob = json.dumps(node_cfg(rec), ensure_ascii=False, indent=2).encode("utf-8")
            b64 = _b64.b64encode(blob).decode()
            rc, out, err = _ssh_run(cli, "echo %s | base64 -d > %s/config.json"
                                         % (b64, _q(d)))
            if rc != 0:
                return "写配置失败：" + ((err or out)[:120])
            _ssh_run(cli, "systemctl restart %s" % unit, timeout=90)
            return "配置已推送并重启生效"
        if action == "remove":
            _ssh_run(cli, "systemctl stop %s; systemctl disable %s >/dev/null 2>&1; "
                          "rm -f /etc/systemd/system/%s.service; systemctl daemon-reload"
                     % (unit, unit, unit), timeout=90)
            _ssh_run(cli, "rm -rf %s" % _q(d), timeout=120)
            return "已停止服务并删除 %s" % d
        return "未知操作"
    except Exception as e:
        return "操作失败：%s" % repr(e)[:200]
    finally:
        try:
            if cli:
                cli.close()
        except Exception:
            pass


def admin_login_html(err=""):
    e = ('<div class="msg err">%s</div>' % esc(err)) if err else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>管理后台 · ' + esc(CONFIG["site_name"]) + '</title>' + ADMIN_STYLE +
        '</head><body><div class="login"><div class="card">'
        '<h2>' + esc(CONFIG["site_name"]) + ' · 控制台</h2>'
        '<p class="sub">ADMIN CONSOLE</p>' + e +
        '<form method="post" action="/admin/login">'
        '<label>管理密码</label><input name="password" type="password" autocomplete="current-password" placeholder="请输入管理密码" required>'
        '<button class="btn" type="submit">登录</button></form>'
        '<p class="tip muted" style="font-size:11.5px;margin:16px 0 0;text-align:center">'
        '<a href="/">返回前台</a></p>'
        '</div></div></body></html>')


SIDE_CSS = """<style>
.shell{display:flex;min-height:100vh}
.side{width:212px;flex:0 0 212px;background:linear-gradient(180deg,#101419,#0a0c0f);
border-right:1px solid var(--line);padding:18px 12px;position:sticky;top:0;height:100vh;overflow:auto}
.side .sb{display:flex;align-items:center;gap:9px;padding:2px 8px 16px;margin-bottom:8px;
border-bottom:1px solid var(--line)}
.side .sb i{width:24px;height:24px;border-radius:7px;background:linear-gradient(135deg,#f0d38a,#c9a144);
box-shadow:0 0 14px rgba(230,194,96,.3)}
.side .sb b{font-size:13.5px}.side .sb em{display:block;font-style:normal;font-size:9.5px;
color:#6f7883;letter-spacing:.16em;margin-top:2px}
.mi{display:flex;align-items:center;gap:9px;height:37px;padding:0 11px;border-radius:9px;
font-size:12.5px;color:#9aa3ad;cursor:pointer;margin-bottom:3px;transition:.15s;user-select:none}
.mi:hover{background:rgba(255,255,255,.04);color:#e9edf2}
.mi.on{background:linear-gradient(135deg,rgba(230,194,96,.16),rgba(230,194,96,.05));
color:#f0d59a;box-shadow:inset 0 0 0 1px rgba(230,194,96,.22)}
.mi s{width:5px;height:5px;border-radius:50%;background:#4b535c;text-decoration:none;display:inline-block}
.mi.on s{background:var(--gold);box-shadow:0 0 8px var(--gold)}
.mi b{margin-left:auto;font-size:10px;font-weight:500;color:#6f7883}
.mi.on b{color:#c8a862}
.side .sf{margin-top:14px;padding:10px 11px;border-radius:9px;background:rgba(255,255,255,.02);
border:1px solid var(--line);font-size:10.5px;color:#6f7883;line-height:1.7}
.main{flex:1;min-width:0;padding:20px 26px 56px;max-width:1180px}
.mtop{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;gap:12px}
.mtop h1{margin:0;font-size:17px;letter-spacing:.02em}
.mtop .acts{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--dim)}
.sec{display:none}.sec.on{display:block}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid3{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:10px;align-items:end}
.fr{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fld{font-size:11px;color:#8b939d;letter-spacing:.06em}
.fld input,.fld select{margin-top:6px}
.inline{display:inline-flex;gap:6px;align-items:center}
pre.log{margin:10px 0 0;padding:10px 12px;border-radius:9px;background:rgba(0,0,0,.42);
border:1px solid var(--line);color:#a9b3bd;font-family:Consolas,Menlo,monospace;
font-size:11.5px;line-height:1.65;max-height:220px;overflow:auto;white-space:pre-wrap}
.hr{height:1px;background:var(--line);margin:18px 0}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.kv .it{background:rgba(255,255,255,.02);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kv .it .k{font-size:10.5px;color:#7d858f;letter-spacing:.1em;margin-bottom:4px}
.kv .it .v{font-size:12.5px;color:#e9edf2;word-break:break-all}
@media(max-width:900px){.shell{flex-direction:column}.side{width:auto;flex:none;height:auto;
position:static;display:flex;flex-wrap:wrap;gap:6px}.side .sb{width:100%}.side .sf{display:none}
.mi{height:32px;padding:0 10px;margin:0}.main{padding:16px}}
</style>"""


def admin_dash_html(msg="", err=""):
    now = time.time()
    d = license_load()
    nodes = load_nodes()

    # ---------------- 授权码表
    lrows = []
    for it in d["codes"]:
        code = esc(it.get("code", ""))
        raw_code = str(it.get("code", ""))
        enabled = it.get("enabled", True)
        exp = (it.get("expire") or "").strip()
        expired = bool(exp) and exp < time.strftime("%Y-%m-%d")
        tag = ('<span class="tag exp">已过期</span>' if expired else
               ('<span class="tag on">启用</span>' if enabled else '<span class="tag off">停用</span>'))
        lrows.append(
            '<tr><td><code class="k">%s</code></td><td>%s</td><td>%s</td>'
            '<td><form method="post" action="/admin/license/expire" class="inline">'
            '<input type="hidden" name="code" value="%s">'
            '<input name="expire" value="%s" placeholder="2027-12-31" style="width:112px;height:27px">'
            '<button class="btn ghost mini" type="submit">保存</button></form></td>'
            '<td>%s</td><td class="muted">%s</td><td style="white-space:nowrap">'
            '<form method="post" action="/admin/license/toggle" style="display:inline">'
            '<input type="hidden" name="code" value="%s">'
            '<button class="btn ghost mini" type="submit">%s</button></form> '
            '<form method="post" action="/admin/license/del" style="display:inline" '
            'onsubmit="return confirm(\'确认删除该授权码？\')">'
            '<input type="hidden" name="code" value="%s">'
            '<button class="btn ghost mini" type="submit">删除</button></form></td></tr>'
            % (code, esc(it.get("note", "") or "-"), esc(it.get("host", "") or "不限"),
               raw_code, esc(exp), tag, esc(it.get("last_used", "") or "-"),
               raw_code, "停用" if enabled else "启用", raw_code))
    license_table = ('<div class="tablewrap"><table><thead><tr><th>授权码</th><th>备注</th>'
                     '<th>绑定域名/IP</th><th>授权到期</th><th>状态</th><th>最近校验</th><th>操作</th>'
                     '</tr></thead><tbody>'
                     + ("".join(lrows) if lrows else
                        '<tr><td colspan="7"><div class="empty">还没有授权码，下方新增即可</div></td></tr>')
                     + '</tbody></table></div>')

    # ---------------- 活跃房间
    rlist = sorted(ROOMS.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:20]
    rrows = []
    for rid, it in rlist:
        age = int(now - it.get("ts", now))
        hot = ('<span class="tag on">%d 人</span>' % it.get("viewers", 0)
               if it.get("viewers") else '<span class="tag off">0 人</span>')
        rrows.append('<tr><td><code class="k">%s</code></td><td>%s</td>'
                     '<td class="muted">%d 秒前</td><td>%d B</td>'
                     '<td><button class="btn ghost mini" type="button" '
                     'onclick="var b=this;navigator.clipboard.writeText(\'%s\').then(function(){'
                     'b.textContent=\'已复制\';setTimeout(function(){b.textContent=\'复制房间号\'},1200)})">'
                     '复制房间号</button></td></tr>'
                     % (esc(str(rid)), hot, age, len(it.get("data", "") or ""), esc(str(rid))))
    rooms_table = ('<div class="tablewrap"><table><thead><tr><th>房间ID</th><th>在线</th>'
                   '<th>最后上报</th><th>数据长度</th><th>操作</th></tr></thead><tbody>'
                   + ("".join(rrows) if rrows else
                      '<tr><td colspan="5"><div class="empty">当前没有客户端上报，房间为空</div></td></tr>')
                   + '</tbody></table></div>')

    # ---------------- 客户端接入
    _cl = list(reversed(load_clients()))[:30]
    crows = []
    for it in _cl:
        try:
            _ts = time.strftime("%m-%d %H:%M", time.localtime(int(it.get("ts") or 0)))
        except Exception:
            _ts = ""
        crows.append(
            '<tr><td class="muted">%s</td><td><code class="k">%s</code></td><td>%s</td>'
            '<td><b>%s</b></td><td>%s</td><td class="muted">%s</td><td class="muted">%s</td></tr>'
            % (esc(_ts), esc(str(it.get("host") or "")), esc(str(it.get("port") or "")),
               esc(str(it.get("room") or "")),
               '<span class="tag on">自定义</span>' if it.get("custom") else
               '<span class="tag off">默认</span>',
               esc(str(it.get("model") or "")), esc(str(it.get("ip") or ""))))
    clients_table = ('<div class="tablewrap"><table><thead><tr><th>时间</th><th>服务器</th>'
                     '<th>端口</th><th>房间</th><th>类型</th><th>机型</th><th>来源IP</th></tr></thead><tbody>'
                     + ("".join(crows) if crows else
                        '<tr><td colspan="7"><div class="empty">还没有 APP 接入记录</div></td></tr>')
                     + '</tbody></table></div>')

    # ---------------- 部署记录
    nrows = []
    for it in reversed(nodes[-40:]):
        st = it.get("status") or "pending"
        tag = {'ok': '<span class="tag on">运行正常</span>',
               'warn': '<span class="tag exp">异常</span>',
               'fail': '<span class="tag exp">失败</span>',
               'running': '<span class="tag off">部署中</span>',
               'stopped': '<span class="tag off">已停止</span>'}.get(st, '<span class="tag off">%s</span>' % esc(st))
        nid = it.get("id")
        wp = it.get("ws_port")
        nrows.append(
            '<tr><td><b>%s</b><br><span class="muted" style="font-size:11px">%s</span></td>'
            '<td><code class="k">%s</code><br><span class="muted" style="font-size:11px">:%s · %s</span></td>'
            '<td>%s</td><td>%s</td><td class="muted">%s</td>'
            '<td class="muted" style="font-size:11px">%s<br>%s</td>'
            '<td style="white-space:nowrap">'
            '<form method="post" action="/admin/node/action" class="inline">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="restart">'
            '<button class="btn ghost mini">重启</button></form> '
            '<form method="post" action="/admin/node/action" class="inline">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="stop">'
            '<button class="btn ghost mini">停止</button></form> '
            '<form method="post" action="/admin/node/action" class="inline">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="push_global">'
            '<button class="btn ghost mini">推弹窗</button></form> '
            '<form method="post" action="/admin/node/action" class="inline">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="lic_off" '
            'onsubmit="return confirm(\'停用后该站点最多 2 分钟内变成授权失效页，确认？\')">'
            '<button class="btn ghost mini">停授权</button></form> '
            '<form method="post" action="/admin/node/action" class="inline">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="lic_on">'
            '<button class="btn ghost mini">恢复授权</button></form> '
            '<form method="post" action="/admin/node/action" class="inline">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="remove" '
            'onsubmit="return confirm(\'确认卸载？会停止服务并删除目录\')">'
            '<button class="btn ghost mini">卸载</button></form> '
            '<form method="post" action="/admin/node/action" class="inline">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="del" '
            'onsubmit="return confirm(\'仅删除记录，不动服务器\')">'
            '<button class="btn ghost mini">删记录</button></form>'
            '</td></tr>'
            % (esc(it.get("site_name") or "-"), esc(it.get("note") or ""),
               esc(str(it.get("host") or "")), esc(str(wp)),
               esc(it.get("ssh_user") or "root"),
               tag, esc(it.get("expire") or "永久"),
               esc(it.get("admin_pass") or "-"),
               time.strftime("%m-%d %H:%M", time.localtime(int(it.get("created") or 0))),
               esc(time.strftime("%m-%d %H:%M", time.localtime(int(it.get("updated") or 0))))
               if it.get("updated") else "-",
               nid, nid, nid, nid, nid, nid, nid))
    nodes_table = ('<div class="tablewrap"><table><thead><tr><th>站点</th><th>SSH / 端口</th>'
                   '<th>状态</th><th>授权到期</th><th>后台密码</th><th>创建 / 更新</th><th>操作</th>'
                   '</tr></thead><tbody>'
                   + ("".join(nrows) if nrows else
                      '<tr><td colspan="7"><div class="empty">还没有部署记录，去「一键部署」填服务器信息</div></td></tr>')
                   + '</tbody></table></div>')

    # 每个节点的远程配置卡片（授权到期 + 弹窗）
    ccards = []
    for it in reversed(nodes[-40:]):
        nid = it.get("id")
        cf = it.get("cfg") or {}
        ccards.append(
            '<div class="card"><h3>%s <em>%s:%s</em></h3>'
            '<p class="tip">远程站点：<a href="http://%s:%s/" target="_blank">http://%s:%s/</a>'
            ' · 后台密码 <code class="k">%s</code> · 状态 %s</p>'
            '<form method="post" action="/admin/node/action">'
            '<input type="hidden" name="id" value="%s"><input type="hidden" name="action" value="push">'
            '<div class="grid3">'
            '<label class="fld">授权到期<input name="expire" value="%s" placeholder="留空=一个月后"></label>'
            '<label class="fld">前台弹窗<select name="popup_on" class="sel">'
            '<option value="1"%s>开启</option><option value="0"%s>关闭</option></select></label>'
            '<label class="fld">弹窗频次<select name="popup_repeat_days" class="sel">'
            '<option value="0"%s>每次都弹</option><option value="1"%s>一天一次</option>'
            '<option value="7"%s>一周一次</option></select></label>'
            '</div>'
            '<div class="fr" style="margin-top:10px">'
            '<label class="fld">弹窗标题<input name="popup_title" value="%s"></label>'
            '<label class="fld">群号<input name="popup_group" value="%s"></label>'
            '</div>'
            '<label class="fld" style="display:block;margin-top:10px">弹窗说明'
            '<input name="popup_desc" value="%s"></label>'
            '<button class="btn" type="submit" style="margin-top:12px">推送到该节点并重启</button>'
            '</form>'
            '<pre class="log">%s</pre></div>'
            % (esc(it.get("site_name") or "未命名"), esc(str(it.get("host"))), esc(str(it.get("web_port") or WEB_PORT)),
               esc(str(it.get("host"))), esc(str(it.get("web_port") or WEB_PORT)),
               esc(str(it.get("host"))), esc(str(it.get("web_port") or WEB_PORT)),
               esc(it.get("admin_pass") or "-"), esc(it.get("status") or "-"),
               nid, esc(it.get("expire") or ""),
               " selected" if cf.get("popup_on", True) else "",
               "" if cf.get("popup_on", True) else " selected",
               " selected" if int(cf.get("popup_repeat_days") or 1) == 0 else "",
               " selected" if int(cf.get("popup_repeat_days") or 1) == 1 else "",
               " selected" if int(cf.get("popup_repeat_days") or 1) == 7 else "",
               esc(str(cf.get("popup_title") or CONFIG.get("popup_title") or "")),
               esc(str(cf.get("popup_group") or CONFIG.get("popup_group") or "")),
               esc(str(cf.get("popup_desc") or CONFIG.get("popup_desc") or "")),
               esc(it.get("log") or "暂无日志")))

    banner = ""
    if msg:
        banner += '<div class="msg ok">%s</div>' % esc(msg)
    if err:
        banner += '<div class="msg err">%s</div>' % esc(err)

    ann_on = bool(CONFIG.get("announce_on", True))
    pop_on = bool(CONFIG.get("popup_on", True))
    srv_mode = ("母站模式：本机不校验授权" if not str(CONFIG.get("license_server") or "").strip()
                else "部署端模式：" + esc(CONFIG.get("license_server")))
    ok_nodes = len([x for x in nodes if x.get("status") == "ok"])

    # ---------------- 概览
    sec_overview = (
        '<div class="sec on" id="sec-overview">'
        '<div class="stats">'
        '<div class="stat"><div class="k">WS 在线</div><div class="v">%d</div></div>'
        '<div class="stat"><div class="k">活跃房间</div><div class="v">%d</div></div>'
        '<div class="stat"><div class="k">今日上行</div><div class="v">%s</div></div>'
        '<div class="stat"><div class="k">今日下行</div><div class="v">%s</div></div>'
        '</div>'
        '<div class="card"><h3>运行概况 <em>OVERVIEW</em></h3>'
        '<div class="kv">'
        '<div class="it"><div class="k">已部署节点</div><div class="v">%d 个（正常 %d）</div></div>'
        '<div class="it"><div class="k">授权码</div><div class="v">%d 个</div></div>'
        '<div class="it"><div class="k">APP 接入记录</div><div class="v">%d 条</div></div>'
        '<div class="it"><div class="k">前台弹窗</div><div class="v">%s</div></div>'
        '<div class="it"><div class="k">公告</div><div class="v">%s</div></div>'
        '<div class="it"><div class="k">授权模式</div><div class="v">%s</div></div>'
        '</div></div>'
        '<div class="card"><h3>最近部署 <em>DEPLOYMENTS</em></h3>%s</div>'
        '<div class="card"><h3>最近客户端 <em>CLIENTS</em></h3>%s</div>'
        '</div>'
        % (len(WS_CLIENTS), len(ROOMS), fmt_bytes(STATS["up"]), fmt_bytes(STATS["down"]),
           len(nodes), ok_nodes, len(d["codes"]), len(load_clients()),
           "开启" if pop_on else "关闭", "开启" if ann_on else "关闭", srv_mode,
           nodes_table, clients_table))

    # ---------------- 站点
    sec_site = (
        '<div class="sec" id="sec-site">'
        '<div class="card"><h3>站点设置 <em>SITE</em></h3>'
        '<p class="tip">站点名称与管理密码，保存立即生效，不用重启。</p>'
        '<form method="post" action="/admin/site">'
        '<div class="fr">'
        '<label class="fld">站点名称<input name="site_name" value="%s"></label>'
        '<label class="fld">新管理密码（留空不改）<input name="admin_pass" autocomplete="new-password"></label>'
        '</div>'
        '<div class="fr" style="margin-top:10px">'
        '<label class="fld" style="flex:1">默认数据源<input name="default_source" value="%s" placeholder="留空=只连本机，如 king.weilua.top:8888"></label>'
        '</div>'
        '<div class="fr" style="margin-top:10px">'
        '<label class="fld" style="flex:1">对外访问地址<input name="public_url" value="%s" placeholder="子站回连母站用，留空自动用 IP:8080；有域名填 http://你的域名/"></label>'
        '</div>'
        '<input type="hidden" name="announce_title" value="%s">'
        '<input type="hidden" name="announce_content" value="%s">'
        '<input type="hidden" name="announce_mode" value="%s">'
        '<input type="hidden" name="announce_repeat" value="%s">'
        '%s'
        '<button class="btn" type="submit" style="margin-top:14px">保存站点</button>'
        '</form></div></div>'
        % (esc(CONFIG.get("site_name") or ""),
           esc(CONFIG.get("default_source") or ""),
           esc(CONFIG.get("public_url") or ""),
           esc(CONFIG.get("announce_title") or ""),
           esc(CONFIG.get("announce_content") or ""),
           esc(CONFIG.get("announce_mode") or "modal"),
           esc(str(CONFIG.get("announce_repeat") or 120)),
           '<input type="hidden" name="announce_on" value="1">' if ann_on else "",
           ))

    # ---------------- 弹窗 / 公告
    sec_popup = (
        '<div class="sec" id="sec-popup">'
        '<div class="card"><h3>资源群弹窗 <em>POPUP</em></h3>'
        '<p class="tip">前台首页的「进群获取更多资源」弹窗。标题支持 <code class="k">&lt;em&gt;</code> 高亮；'
        '频次控制用户多久看到一次（客户端 localStorage 记忆）。</p>'
        '<form method="post" action="/admin/popup">'
        '<div class="grid3">'
        '<label class="fld">开关<select name="popup_on" class="sel">'
        '<option value="1"%s>开启</option><option value="0"%s>关闭</option></select></label>'
        '<label class="fld">频次<select name="popup_repeat_days" class="sel">'
        '<option value="0"%s>每次都弹</option><option value="1"%s>一天一次</option>'
        '<option value="7"%s>一周一次</option></select></label>'
        '<label class="fld">群号<input name="popup_group" value="%s"></label>'
        '</div>'
        '<div class="fr" style="margin-top:10px">'
        '<label class="fld">眉标<input name="popup_eyebrow" value="%s"></label>'
        '<label class="fld">标题（可含 HTML）<input name="popup_title" value="%s"></label>'
        '</div>'
        '<label class="fld" style="display:block;margin-top:10px">说明<input name="popup_desc" value="%s"></label>'
        '<label class="fld" style="display:block;margin-top:10px">底注<input name="popup_tip" value="%s"></label>'
        '<button class="btn" type="submit" style="margin-top:14px">保存弹窗</button>'
        '</form></div>'
        '<div class="card"><h3>系统公告 <em>ANNOUNCEMENT</em></h3>'
        '<p class="tip">走 <code class="k">/api/announcement</code>，前台弹窗或顶部横幅。</p>'
        '<form method="post" action="/admin/site">'
        '<input type="hidden" name="site_name" value="%s">'
        '<div class="grid3">'
        '<label class="fld">标题<input name="announce_title" value="%s"></label>'
        '<label class="fld">形态<select name="announce_mode" class="sel">'
        '<option value="modal"%s>弹窗提醒</option><option value="banner"%s>顶部横幅</option></select></label>'
        '<label class="fld">重复间隔（分钟）<input name="announce_repeat" type="number" min="0" max="10080" value="%s"></label>'
        '</div>'
        '<label class="chk" style="margin-top:10px"><input type="checkbox" name="announce_on" value="1"%s> 前台显示公告</label>'
        '<label class="fld" style="display:block;margin-top:10px">内容（HTML）'
        '<textarea name="announce_content" rows="4">%s</textarea></label>'
        '<button class="btn" type="submit" style="margin-top:14px">保存公告</button>'
        '</form></div></div>'
        % (" selected" if pop_on else "", "" if pop_on else " selected",
           " selected" if int(CONFIG.get("popup_repeat_days") or 1) == 0 else "",
           " selected" if int(CONFIG.get("popup_repeat_days") or 1) == 1 else "",
           " selected" if int(CONFIG.get("popup_repeat_days") or 1) == 7 else "",
           esc(CONFIG.get("popup_group") or ""), esc(CONFIG.get("popup_eyebrow") or ""),
           esc(CONFIG.get("popup_title") or ""), esc(CONFIG.get("popup_desc") or ""),
           esc(CONFIG.get("popup_tip") or ""),
           esc(CONFIG.get("site_name") or ""), esc(CONFIG.get("announce_title") or ""),
           " selected" if str(CONFIG.get("announce_mode") or "modal") == "modal" else "",
           " selected" if str(CONFIG.get("announce_mode")) == "banner" else "",
           esc(str(CONFIG.get("announce_repeat") or 120)),
           " checked" if ann_on else "",
           esc(CONFIG.get("announce_content") or "")))

    # ---------------- 房间
    sec_rooms = (
        '<div class="sec" id="sec-rooms">'
        '<div class="card"><h3>活跃房间 <em>LIVE ROOMS</em></h3>'
        '<p class="tip">按最后上报时间排序，最多 20 个。复制房间号到前台「共享房间」搜索框即可进入。</p>'
        + rooms_table + '</div></div>')

    # ---------------- 客户端
    sec_clients = (
        '<div class="sec" id="sec-clients">'
        '<div class="card"><h3>客户端接入 <em>APP CLIENTS</em></h3>'
        '<p class="tip">APP 连上就自动上报一条（同节点同房间 20 分钟内去重），最多留 800 条，这里显示最近 30 条。</p>'
        + clients_table + '</div></div>')

    # ---------------- 授权
    sec_license = (
        '<div class="sec" id="sec-license">'
        '<div class="card"><h3>授权管理 <em>LICENSE</em></h3>'
        '<p class="tip"><b style="color:#ffb020">当前为免授权版（LICENSE_OFF=True）：下面的授权码/到期日不再参与校验，'
        '子站部署随便填、留空都能跑。</b><br>想恢复授权体系，把源码顶部 LICENSE_OFF 改成 False 重启即可，'
        '这里的卡密会立刻重新生效。当前模式：%s</p>'
        '<form method="post" action="/admin/license/add">'
        '<div class="fr">'
        '<label class="fld">类型<select name="kind" class="sel">'
        '<option value="site">站点授权码</option>'
        '<option value="deploy">部署卡密（一次性）</option></select></label>'
        '<label class="fld">授权码（留空自动生成）<input name="code" placeholder="KV-XXXX-XXXX-XXXX"></label>'
        '<label class="fld">备注<input name="note" placeholder="客户A"></label>'
        '</div>'
        '<div class="fr" style="margin-top:10px">'
        '<label class="fld">绑定域名/IP（留空不限）<input name="host" placeholder="example.com"></label>'
        '<label class="fld">到期日<input name="expire" placeholder="留空=一个月后"></label>'
        '</div>'
        '<button class="btn" type="submit" style="margin-top:14px">新增授权码</button>'
        '</form>'
        '<div class="hr"></div>'
        '<h4 style="margin:0 0 10px;font-size:13px;color:#9aa1ab">批量生成 <em style="font-style:normal;color:#e6c260;font-size:10px;letter-spacing:2px">BATCH</em></h4>'
        '<form method="post" action="/admin/license/batch">'
        '<div class="grid3">'
        '<label class="fld">数量<input name="count" value="10" inputmode="numeric"></label>'
        '<label class="fld">类型<select name="kind" class="sel">'
        '<option value="site">站点授权码</option>'
        '<option value="deploy">部署卡密（一次性）</option></select></label>'
        '<label class="fld">有效期<select name="days" class="sel">'
        '<option value="5">5 天</option>'
        '<option value="7">7 天</option>'
        '<option value="30" selected>30 天</option>'
        '<option value="60">60 天</option>'
        '<option value="90">90 天</option>'
        '<option value="180">180 天</option>'
        '<option value="365">1 年</option>'
        '<option value="0">永久</option></select></label>'
        '</div>'
        '<div class="fr" style="margin-top:10px">'
        '<label class="fld">备注前缀<input name="note" placeholder="客户批量A（可留空）"></label>'
        '</div>'
        '<button class="btn" type="submit" style="margin-top:14px">批量生成</button>'
        '</form>'
        '<div class="hr"></div>' + license_table + '</div></div>') % srv_mode

    # ---------------- 一键部署
    sec_deploy = (
        '<div class="sec" id="sec-deploy">'
        '<div class="card"><h3>一键部署 <em>ONE-CLICK DEPLOY</em></h3>'
        '<p class="tip">填目标服务器的 SSH 信息，系统会通过 SSH 自动完成：装 python3 → 上传本站程序与前台 → '
        '写 systemd 服务 → 启动并验证端口 → 自动生成授权码（不限域名，换 IP 也不锁）。'
        '需要目标机 root（或能 sudo 的账号）与 22 端口可达。</p>'
        '<form method="post" action="/admin/deploy">'
        '<div class="grid3">'
        '<label class="fld">部署方式<select name="mode" class="sel">'
        '<option value="ssh">远程 SSH（客户服务器）</option>'
        '<option value="local">本机（免 SSH，直接开新实例）</option></select></label>'
        '<label class="fld">SSH 端口<input name="ssh_port" value="22"></label>'
        '<label class="fld">SSH 账号<input name="ssh_user" value="root"></label>'
        '</div>'
        '<div class="fr" style="margin-top:10px">'
        '<label class="fld">SSH 密码<input name="ssh_pass" type="password" placeholder="root 密码（本机部署留空）"></label>'
        '<label class="fld">站点名称<input name="site_name" value="%s"></label>'
        '</div>'
        '<div class="grid3" style="margin-top:10px">'
        '<label class="fld">安装目录<input name="dir" value="/www/wwwroot/king-vision"></label>'
        '<label class="fld">网页端口（固定 8080 · 网页/后台）<input name="web_port" value="8080" readonly style="opacity:.65;cursor:not-allowed"></label>'
        '<label class="fld">数据端口（WS · 固定 8888）<input name="ws_port" value="8888" readonly style="opacity:.65;cursor:not-allowed"></label>'
        '<label class="fld">上报端口（TCP · 固定 9999）<input name="tcp_port" value="9999" readonly style="opacity:.65;cursor:not-allowed"></label>'
        '</div>'
        '<div class="grid3" style="margin-top:10px">'
        '<label class="fld">该站后台密码<input name="admin_pass" placeholder="留空自动生成"></label>'
        '<label class="fld">授权到期<input name="expire" placeholder="留空=一个月后"></label>'
        '<label class="fld">备注<input name="note" placeholder="客户A / 测试机"></label>'
        '</div>'
        '<button class="btn" type="submit" style="margin-top:16px">开始一键部署</button>'
        '<p class="tip muted" style="margin:12px 0 0;font-size:11.5px">'
        '部署过程是后台任务，提交后可在「部署记录」里看实时日志。</p>'
        '</form></div></div>') % esc(CONFIG.get("site_name") or "强哥公益雷达")

    # ---------------- 部署记录
    sec_nodes = (
        '<div class="sec" id="sec-nodes">'
        '<div class="card"><h3>部署记录 <em>NODES</em></h3>'
        '<p class="tip">每个节点都能远程重启 / 停止 / 卸载，并单独控制它的授权到期和前台弹窗。'
        '「推弹窗」会把「弹窗 / 公告」页当前的配置同步到该节点并重启生效。</p>'
        '<form method="post" action="/admin/node/action" style="margin:0 0 14px">'
        '<input type="hidden" name="action" value="push_all">'
        '<button class="btn ghost mini" type="submit">把当前弹窗配置推送到全部节点</button></form>'
        + nodes_table + '</div>'
        + ("".join(ccards) if ccards else
           '<div class="card"><div class="empty">还没有节点</div></div>')
        + '</div>')

    nav = [
        ("overview", "概览", "DASH"),
        ("site", "站点设置", ""),
        ("popup", "弹窗 / 公告", ""),
        ("rooms", "活跃房间", str(len(ROOMS))),
        ("clients", "客户端接入", str(len(load_clients()))),
        ("license", "授权管理", str(len(d["codes"]))),
        ("deploy", "一键部署", ""),
        ("nodes", "部署记录", str(len(nodes))),
    ]
    nav_html = "".join(
        '<div class="mi%s" data-sec="%s"><s></s>%s<b>%s</b></div>'
        % (" on" if k == "overview" else "", k, esc(t), esc(b) if b else "")
        for k, t, b in nav)

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>管理后台 · ' + esc(CONFIG["site_name"]) + '</title>'
        + ADMIN_STYLE + SIDE_CSS +
        '</head><body><div class="shell">'
        '<aside class="side"><div class="sb"><i></i><div><b>' + esc(CONFIG["site_name"]) +
        '</b><em>ADMIN CONSOLE</em></div></div>' + nav_html +
        '<div class="sf">母站 ' + esc(public_host()) + '<br>v1.2.2 · relay-lite</div></aside>'
        '<main class="main">'
        '<div class="mtop"><h1 id="mtitle">概览</h1>'
        '<div class="acts"><a href="/" target="_blank">前台</a>'
        '<span class="muted">|</span><a href="/admin/logout">退出</a></div></div>'
        + banner + sec_overview + sec_site + sec_popup + sec_rooms + sec_clients +
        sec_license + sec_deploy + sec_nodes +
        '</main></div>'
        '<script>'
        'var T={overview:"概览",site:"站点设置",popup:"弹窗 / 公告",rooms:"活跃房间",'
        'clients:"客户端接入",license:"授权管理",deploy:"一键部署",nodes:"部署记录"};'
        'function go(k){if(!T[k])k="overview";'
        'document.querySelectorAll(".mi").forEach(function(e){e.classList.toggle("on",e.dataset.sec===k)});'
        'document.querySelectorAll(".sec").forEach(function(e){e.classList.toggle("on",e.id==="sec-"+k)});'
        'document.getElementById("mtitle").textContent=T[k];'
        'try{history.replaceState(null,"","#"+k)}catch(e){}window.scrollTo(0,0);}'
        'document.querySelectorAll(".mi").forEach(function(e){e.onclick=function(){go(e.dataset.sec)}});'
        'go((location.hash||"").replace("#","")||"overview");'
        'if(' + ('1' if any(x.get("status") == "running" for x in nodes) else '0') +
        '){setTimeout(function(){location.reload()},4000);}'
        '</script>'
        '</body></html>')


def fmt_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%.0f %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def _guess_host():
    """没配 PUBLIC_HOST 时用本机出站地址兜底（云主机多半是内网 IP，别指望它对外可达）。"""
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            _s.connect(("223.5.5.5", 80))
            return _s.getsockname()[0]
        finally:
            _s.close()
    except Exception:
        return "127.0.0.1"


HOST_HINT = (os.environ.get("PUBLIC_HOST") or "").strip() or _guess_host()


def public_host():
    """子站回连母站用的主机名：public_url 优先，其次 PUBLIC_HOST，最后本机地址。"""
    pu = str(CONFIG.get("public_url") or "").strip()
    if pu:
        s = pu.replace("https://", "").replace("http://", "").split("/")[0].strip()
        if s:
            return s.split(":")[0]
    return HOST_HINT


def build_source_zip():
    """把当前服务与站点打包成部署包，返回字节。"""
    import io as _io
    buf = _io.BytesIO()
    root = os.path.join(BASE_DIR, "dist")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.abspath(__file__), "king-vision-lite/king-relay-lite.py")
        if os.path.isdir(root):
            for base, _dirs, files in os.walk(root):
                for fn in files:
                    full = os.path.join(base, fn)
                    rel = os.path.relpath(full, root).replace("\\", "/")
                    if rel.startswith("source/"):
                        continue
                    z.write(full, "king-vision-lite/dist/" + rel)
        z.writestr("king-vision-lite/config.json", json.dumps({
            "_说明": "admin_pass 留空即可：首次启动会自动生成随机管理密码，见 journalctl -u king-relay-lite 或日志文件",
            "site_name": CONFIG["site_name"],
            "admin_user": CONFIG["admin_user"],
            "admin_pass": "",
            "license_server": "",
            "license_code": "",
            "_说明2": "本版本为免授权版，license_server/license_code 留空即可，不需要任何卡密",
        }, ensure_ascii=False, indent=2))
        z.writestr("king-vision-lite/install.sh", INSTALL_SH)
        z.writestr("king-vision-lite/README.md", SOURCE_README)
    return buf.getvalue()


INSTALL_SH = """#!/usr/bin/env bash
# 强哥公益雷达同款 · 一键部署（需 root）
set -e
DIR=${DIR:-/www/wwwroot/king}
echo "== 1) 拷贝文件到 $DIR"
mkdir -p "$DIR"
cp -r ./dist "$DIR"/ 2>/dev/null || true
cp ./king-relay-lite.py ./config.json "$DIR"/
mkdir -p "$DIR/data"
echo "== 2) 安装 systemd"
cat > /etc/systemd/system/king-relay-lite.service <<'EOF'
[Unit]
Description=King Vision Relay Lite
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/www/wwwroot/king
Environment=WS_PORT=8888
Environment=TCP_PORT=9999
Environment=WEB_PORT=8080
Environment=STATIC_DIR=/www/wwwroot/king/dist
ExecStart=/usr/bin/python3 -u /www/wwwroot/king/king-relay-lite.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now king-relay-lite
echo "== 3) 完成，访问 http://<本机IP>:8080/  后台 http://<本机IP>:8080/admin"
echo "   本包为免授权版：不需要任何卡密/授权码，直接跑"
echo "   管理密码：config.json 里 admin_pass 留空时，服务会自动生成一个随机密码"
echo "   查看方式：journalctl -u king-relay-lite | grep 初始管理密码"
"""

SOURCE_README = """# 强哥公益雷达同款 · 部署包

## 目录
- `king-relay-lite.py`  中转服务（网页 8080 / 数据 WS 8888 / 上报 TCP 9999）
- `dist/`               前台 `index/` 与后台 `admin/`
- `config.json`         站点名、管理密码（免授权版不写授权服务器）
- `install.sh`          systemd 安装脚本

## 免授权
本包内置 `LICENSE_OFF = True`：不需要授权码、不需要部署卡密、没有到期时间、没有未授权拦截页，
TCP 上报口直接开放。`config.json` 里的 `license_server` / `license_code` 留空即可。
想恢复授权体系，把 `king-relay-lite.py` 顶部 `LICENSE_OFF` 改成 `False` 重启。

## 部署步骤
1. 上传解压到 `/www/wwwroot/king`
2. 改 `config.json`（只需改站点名/管理密码，其余可不动）
3. `bash install.sh`
4. nginx 反代到 `127.0.0.1:8080`（网页）；`8888/9999` 只跑游戏数据，不要反代
5. 浏览器打开 `http://<域名>:8080/`，后台 `/admin`

## 管理密码
`config.json` 里 `admin_pass` 留空，服务首次启动会生成一个随机密码并写入日志：

```bash
journalctl -u king-relay-lite | grep 初始管理密码
# 或
grep 初始管理密码 /var/log/king-relay-lite.log
```

登录后台后可在「站点设置」里改成自己的密码。也可以部署前直接在 `config.json` 里填好 `admin_pass`。

## 授权
本包为免授权版（`LICENSE_OFF = True`），任何页面都不会出现未授权提示，TCP 上报口始终接收数据。
后台「授权管理」页仍在，但里面的卡密/到期日不再参与校验。
"""


HERO_DIR = os.path.join(DATA_DIR, "hero")
HERO_REMOTE = "https://game.gtimg.cn/images/yxzj/img201606/heroimg/%s/%s.jpg"
SKIN_DIR = os.path.join(DATA_DIR, "skin")
SKIN_REMOTE = "https://game.gtimg.cn/images/yxzj/img201606/skin/hero-info/%s/%s-bigskin-1.jpg"
# 远端缺失时的占位图（8x8 站点底色 #161a20，拉伸后是纯色块，不出破图也不出 404）
PLACEHOLDER_IMG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEUlEQVR42mMQk1LAihiGlgQA"
    "zrEUARH9yqkAAAAASUVORK5CYII=")
# 远端 404 记忆表（1 小时内不重复请求）
_MISS = {}


def _proxy_image(writer, hid, cache_dir, remote, name):
    """同源图片代理 + 本地缓存（头像 / 皮肤大图共用）。"""
    if not re.fullmatch(r"\d{1,4}", hid or ""):
        http_response(writer, 404, "bad id")
        return
    path = os.path.join(cache_dir, hid + ".jpg")
    miss = _MISS.get(path, 0)
    if not (os.path.isfile(path) and os.path.getsize(path) > 500) and time.time() - miss > 3600:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            req = urllib.request.Request(
                remote % (hid, hid),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pvp.qq.com/"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read()
            if len(data) > 500:
                with open(path, "wb") as f:
                    f.write(data)
            else:
                _MISS[path] = time.time()
        except Exception as e:
            _MISS[path] = time.time()
            log("%s拉取失败 %s: %r" % (name, hid, e))
    if os.path.isfile(path):
        with open(path, "rb") as f:
            body = f.read()
        http_response(writer, 200, body, "image/jpeg", cache="public, max-age=604800")
    else:
        # 远端没有这张图（新英雄编号差异等），回透明占位图，避免破图与 404 噪音
        http_response(writer, 200, PLACEHOLDER_IMG, "image/png", cache="public, max-age=3600")


def serve_hero(writer, hid):
    _proxy_image(writer, hid, HERO_DIR, HERO_REMOTE, "头像")


def serve_skin(writer, hid):
    _proxy_image(writer, hid, SKIN_DIR, SKIN_REMOTE, "皮肤图")


def safe_path(url_path):
    p = url_path.split("?")[0].split("#")[0]
    p = posixpath.normpath(p)
    if p in ("/", ""):
        p = "/index.html"
    if p.startswith("/admin") and not p.startswith("/admin/"):
        p = "/admin/index.html"
    if p.endswith("/"):
        p += "index.html"
    # 前端 index 与 admin 分布在不同子目录
    if p.startswith("/admin/"):
        full = os.path.join(STATIC_DIR, "admin", p[len("/admin/"):])
    else:
        full = os.path.join(STATIC_DIR, "index", p.lstrip("/"))
    return os.path.abspath(full)


CLIENT_FILE = os.path.join(DATA_DIR, "clients.json")


def load_clients():
    try:
        with open(CLIENT_FILE, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def save_clients(items):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CLIENT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[-800:], f, ensure_ascii=False)
        os.replace(tmp, CLIENT_FILE)
    except Exception as e:
        log("写入客户端记录失败:", repr(e))


def api_response(writer, path, body=b"", headers=None):
    """最小接口层：去广告 + 统计 + 授权校验。"""
    p = path.split("?")[0].rstrip("/")
    q = {}
    if "?" in path:
        q = dict(urllib.parse.parse_qsl(path.split("?", 1)[1], keep_blank_values=True))
    if not q and body:
        q = parse_form(body)
    if p == "/api/client/report":
        def g(k, d=""):
            v = q.get(k)
            return (v if isinstance(v, str) else d)[:120]
        try:
            peer = writer.get_extra_info("peername")
            ip = peer[0] if peer else ""
        except Exception:
            ip = ""
        try:
            port = int(g("port", "0") or 0)
        except Exception:
            port = 0
        rec = {
            "ts": int(time.time()),
            "host": g("host"),
            "port": port,
            "room": g("room"),
            "custom": 1 if g("custom", "0") == "1" else 0,
            "model": g("model"),
            "brand": g("brand"),
            "ver": g("ver"),
            "ip": ip,
        }
        items = load_clients()
        items.append(rec)
        save_clients(items)
        log("客户端接入 %s:%s 房间=%s 自定义=%s %s %s" %
            (rec["host"], rec["port"], rec["room"], rec["custom"], rec["model"], rec["ip"]))
        http_response(writer, 200, '{"code":200}', "application/json; charset=utf-8")
        return
    if p in ("/api/license/verify", "/api/license/check", "/api/license/query"):
        code = q.get("code") or q.get("license_code") or q.get("key") or ""
        host = q.get("host") or q.get("domain") or q.get("ip") or ""
        try:
            _peer = writer.get_extra_info("peername")
            _rip = _peer[0] if _peer else ""
        except Exception:
            _rip = ""
        _xff = str((headers or {}).get("x-forwarded-for") or "").split(",")[0].strip()
        if _xff:
            _rip = _xff
        ok, reason, exp = license_verify(code, host, _rip)
        log("授权校验 %s host=%r ip=%s -> %s %s"
            % (code[:24], host, _rip, "ok" if ok else "fail", reason))
        http_response(writer, 200, json.dumps({
            "result": "ok" if ok else "fail",
            "reason": reason,
            "appName": "king-vision-lite",
            "expireAt": exp,
        }, ensure_ascii=False), "application/json; charset=utf-8")
        return
    if p in ("/api/promotions", "/api/recommend", "/api/ads"):
        http_response(writer, 200, "[]", "application/json; charset=utf-8")
        return
    if p == "/api/traffic/today":
        reset_stats_daily()
        http_response(writer, 200,
                      json.dumps({"upload_bytes": STATS["up"],
                                  "download_bytes": STATS["down"]}),
                      "application/json; charset=utf-8")
        return
    if p == "/api/room-layout":
        http_response(writer, 200, '{"layout":"list"}',
                      "application/json; charset=utf-8")
        return
    if p == "/api/downloads":
        http_response(writer, 200, '{"enabled":false,"android_url":"","ios_url":""}',
                      "application/json; charset=utf-8")
        return
    if p == "/api/rooms":
        now = time.time()
        rooms = []
        for rid, item in list(ROOMS.items()):
            if not (item.get("data") or "").strip("-= "):
                continue
            ts = float(item.get("ts") or 0)
            if now - ts > ROOM_TTL:
                continue
            rooms.append({
                "id": str(rid),
                "viewers": int(item.get("viewers") or 0),
                "age": int(max(0, now - ts)),
            })
        rooms.sort(key=lambda x: (0, int(x["id"])) if x["id"].isdigit() else (1, x["id"]))
        http_response(writer, 200,
                      json.dumps({"code": 200, "count": len(rooms), "rooms": rooms},
                                 ensure_ascii=False),
                      "application/json; charset=utf-8")
        return
    if p == "/api/announcement":
        on = bool(CONFIG.get("announce_on", True))
        mode = str(CONFIG.get("announce_mode") or "modal")
        body = {
            "title": (CONFIG.get("announce_title") or "系统公告") if on else "",
            "content": (CONFIG.get("announce_content") or "") if on else "",
            "published": on,
            "mode": mode if on else "banner",
            "modal_repeat_minutes": int(CONFIG.get("announce_repeat") or 120),
            "updated_at": str(CONFIG.get("announce_updated") or "") or
                          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        http_response(writer, 200, json.dumps(body, ensure_ascii=False),
                      "application/json; charset=utf-8")
        return
    if p.startswith("/api/admin"):
        http_response(writer, 401, '{"error":"admin login required"}',
                      "application/json; charset=utf-8")
        return
    http_response(writer, 404, '{"error":"not found"}',
                  "application/json; charset=utf-8")


def reset_stats_daily():
    today = time.strftime("%Y-%m-%d")
    if STATS.get("day") != today:
        STATS["day"] = today
        STATS["up"] = 0
        STATS["down"] = 0


def serve_app(writer):
    """下发安卓客户端：优先 APP_FILE，其次 dist 下几个常见文件名。"""
    path = APP_FILE
    if not os.path.isfile(path):
        for alt in (
            os.path.join(STATIC_DIR, "king.apk"),
            os.path.join(STATIC_DIR, "app", "king.apk"),
            os.path.join(BASE_DIR, "app.apk"),
            os.path.join(BASE_DIR, "共享app", "强哥公益雷达.apk"),
        ):
            if os.path.isfile(alt):
                path = alt
                break
    if not os.path.isfile(path):
        http_response(writer, 404, "客户端暂未上传，请联系管理员")
        return
    try:
        size = os.path.getsize(path)
        mtime = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(os.path.getmtime(path)))
        name = urllib.parse.quote("强哥公益雷达.apk")
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Server: nginx\r\n"
            "Content-Type: application/vnd.android.package-archive\r\n"
            "Content-Length: %d\r\n"
            "Last-Modified: %s\r\n"
            "Content-Disposition: attachment; filename=\"king-share.apk\"; filename*=UTF-8''%s\r\n"
            "Cache-Control: public, max-age=600\r\n"
            "Connection: close\r\n\r\n"
        ) % (size, mtime, name)
        writer.write(head.encode("latin1"))
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
        STATS["down"] += size
        log("APP 下载 %d 字节" % size)
    except Exception as e:
        log("APP 下发失败:", repr(e))


def serve_static(writer, path):
    raw = path.split("?")[0]
    # 授权说明页 / 安装向导页不再对外，直接回首页
    if re.match(r"^/(support|setup)(/|$)", raw):
        http_response(writer, 302, "", extra="Location: /\r\n")
        return
    full = safe_path(path)
    if not os.path.isfile(full):
        # SPA 回退
        base = "admin" if path.startswith("/admin") else "index"
        full = os.path.join(STATIC_DIR, base, "index.html")
        if not os.path.isfile(full):
            http_response(writer, 404, "not found")
            return
    ext = os.path.splitext(full)[1].lower()
    ctype = MIME.get(ext, "application/octet-stream")
    with open(full, "rb") as f:
        body = f.read()
    if ext == ".html":
        text = body.decode("utf-8", "ignore")
        # 去掉指向授权站/推荐位的静态引用
        text = re.sub(r'<link[^>]+(?:seayun\.cn)[^>]*>', "", text, flags=re.I)
        inject = AD_GUARD
        # 前台首页挂资源群弹窗，后台不弹
        if "kv-pop-style" not in text and not full.replace("\\", "/").split("/dist/")[-1].startswith("admin/"):
            _dsrc = json.dumps(CONFIG.get("default_source") or "", ensure_ascii=False)
            _ad = AD_DATASOURCE
            if CONFIG.get("child_mode"):
                _ad = _ad.replace("var _CHILD=0;", "var _CHILD=1;")
            inject = (AD_GUARD + popup_html()
                      + _ad.replace("var _DEF_SRC='';", "var _DEF_SRC=%s;" % _dsrc)
                      + APP_DL)
        if AD_GUARD.strip() not in text:
            if "</head>" in text:
                text = text.replace("</head>", inject + "</head>", 1)
            else:
                text = inject + text
        body = text.encode()
    http_response(writer, 200, body, ctype)


async def handle_ws(reader, writer, headers):
    key = headers.get("sec-websocket-key", "")
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    writer.write(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
        ).encode()
    )
    await writer.drain()
    conn = WSConn(writer)
    WS_CLIENTS.add(conn)
    log("WS 连接建立，在线 %d" % len(WS_CLIENTS))
    try:
        while True:
            hdr = await reader.readexactly(2)
            b1, b2 = hdr[0], hdr[1]
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", await reader.readexactly(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", await reader.readexactly(8))[0]
            mask = await reader.readexactly(4) if masked else b"\x00\x00\x00\x00"
            data = bytearray(await reader.readexactly(ln)) if ln else bytearray()
            if masked:
                for i in range(len(data)):
                    data[i] ^= mask[i & 3]
            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping
                writer.write(struct.pack("!BB", 0x8A, len(data)) + bytes(data))
                await writer.drain()
                continue
            if opcode not in (0x1, 0x2, 0x0):
                continue
            msg = bytes(data).decode("utf-8", "ignore").strip()
            if not msg:
                continue
            if msg in ("subscribeHome", "getHome"):
                conn.home = True
                await ws_send(writer, home_payload())
            elif msg.startswith("subscribe[==]"):
                room = msg.split("[==]", 1)[1].strip()
                conn.room = room
                conn.home = False
                r = ROOMS.get(room)
                if r and time.time() - r["ts"] <= ROOM_TTL:
                    await ws_send(writer, "gameData##" + r["data"])
            elif msg.startswith("unsubscribe"):
                conn.room = None
                conn.home = True
            elif msg.startswith("ping##"):
                await ws_send(writer, "pong##" + msg[6:])
            else:
                # 兼容旧协议：<roomId>[==]<target>
                parts = msg.split("[==]")
                if len(parts) >= 2 and parts[1].strip():
                    conn.room = parts[1].strip()
                    r = ROOMS.get(conn.room)
                    if r:
                        await ws_send(writer, "gameData##" + r["data"])
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        log("WS 异常:", repr(e))
    finally:
        WS_CLIENTS.discard(conn)
        try:
            writer.close()
        except Exception:
            pass
        log("WS 断开，在线 %d" % len(WS_CLIENTS))


def parse_form(body):
    try:
        text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else str(body)
        return dict(urllib.parse.parse_qsl(text, keep_blank_values=True))
    except Exception:
        return {}


# ---------------- 公开自助部署 ----------------
DEPLOY_HITS = {}

_DEPLOY_CSS = """
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(1200px 600px at 50% -10%,#16202b 0%,#0b0f14 55%,#070a0d 100%);
color:#e8e6e3;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;padding:28px 16px}
.card{width:min(560px,100%);background:linear-gradient(158deg,#151a21,#0d1116);border:1px solid rgba(230,194,96,.26);
border-radius:16px;padding:28px 26px;box-shadow:0 26px 64px rgba(0,0,0,.6)}
h1{margin:0 0 4px;font-size:19px;letter-spacing:.5px}
h1 em{font-style:normal;color:#e6c260;font-size:11px;letter-spacing:2px;margin-left:8px}
.sub{margin:0 0 20px;color:#8b929c;font-size:12.5px}
.row{display:flex;gap:10px;margin-bottom:12px}
label{display:block;flex:1;font-size:12px;color:#9aa1ab}
label span{display:block;margin-bottom:5px}
input{width:100%;height:38px;padding:0 11px;border-radius:8px;border:1px solid #2c343f;background:#0a0d11;
color:#e8e6e3;font:13px/1 ui-monospace,Consolas,monospace;outline:none}
input:focus{border-color:#e6c260}
.btn{width:100%;height:42px;margin-top:8px;border:0;border-radius:9px;cursor:pointer;
background:linear-gradient(180deg,#f2d783,#d9b455);color:#171717;font-weight:700;font-size:14px;letter-spacing:1px}
.btn:active{transform:translateY(1px)}
.err{margin:0 0 14px;padding:9px 12px;border-radius:8px;background:rgba(217,83,79,.12);
border:1px solid rgba(217,83,79,.4);color:#f0938f;font-size:12.5px}
.tip{margin:16px 0 0;color:#6f7883;font-size:11.5px;line-height:1.7}
.tip code{color:#c9a94e;font-family:ui-monospace,Consolas,monospace}
.log{margin-top:16px;max-height:300px;overflow:auto;background:#070a0d;border:1px solid #222a33;
border-radius:8px;padding:12px;font:11.5px/1.7 ui-monospace,Consolas,monospace;color:#9fb0a8;white-space:pre-wrap}
.ok{color:#5ec27a}
.warn{color:#e6c260}
.fail{color:#ef8a86}
.kv{display:flex;gap:8px;margin:8px 0;font-size:13px}
.kv b{color:#8b929c;font-weight:400;min-width:76px}
.kv span{color:#e8e6e3;font-family:ui-monospace,Consolas,monospace}
"""

def deploy_page(err="", body="", d=None):
    d = d or {}

    def _v(k, dv=""):
        return esc(str(d.get(k) if d.get(k) not in (None, "") else dv))
    e = ('<p class="err">%s</p>' % err) if err else ""
    return ("<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>部署同款 · 强哥公益雷达</title><style>%s</style></head><body>"
            "<div class=card><h1>部署同款<em>ONE-CLICK</em></h1>"
            "<p class=sub>填写你的服务器信息，自动安装一套同款站点。</p>"
            "%s"
            "<form method=post action=/deploy>"
            "<div class=row>"
            "<label><span>服务器 IP / 域名</span><input name=host placeholder='1.2.3.4' value='" + _v("host") + "' required></label>"
            "<label style='flex:0 0 96px'><span>SSH 端口</span><input name=ssh_port value='" + _v("ssh_port", "22") + "'></label>"
            "</div>"
            "<div class=row>"
            "<label><span>SSH 账号</span><input name=ssh_user value='" + _v("ssh_user", "root") + "'></label>"
            "<label><span>SSH 密码</span><input name=ssh_pass type=password placeholder='root 密码' value='" + _v("ssh_pass") + "' required></label>"
            "</div>"
            "<div class=row>"
            "<label style='flex:1'><span>部署卡密（免授权，可留空）</span><input name=deploy_code placeholder='留空即可' value='" + (_v("deploy_code") or ("FREE" if LICENSE_OFF else "")) + "'></label>"
            "</div>"
            "<div class=row>"
            "<label><span>站点名称（可留空）</span><input name=site_name placeholder='强哥公益雷达' value='" + _v("site_name") + "'></label>"
            "<label><span>安装目录</span><input name=dir value='" + _v("dir", "/www/wwwroot/king-vision") + "'></label>"
            "</div>"
            "<div class=row>"
            "<label style='flex:0 0 110px'><span>网页端口</span><input name=web_port value='8080' readonly></label>"
            "<label style='flex:0 0 110px'><span>数据端口</span><input name=ws_port value='8888' readonly></label>"
            "<label style='flex:0 0 110px'><span>上报端口</span><input name=tcp_port value='9999' readonly></label>"
            "<label><span>备注（可留空）</span><input name=note placeholder='客户名' value='" + _v("note") + "'></label>"
            "</div>"
            "<button class=btn type=submit>开始部署</button>"
            "</form>"
            "<p class=tip>部署完成后会给出站点地址与后台密码。<br>"
            "服务器需为 Linux，能 <code>ssh</code> 登录，且 <code>python3</code> 可用。<br>"
            "网页走 8080，游戏数据走 8888/9999，三个端口都要在安全组放行。</p>"
            "</div>%s</body></html>") % (_DEPLOY_CSS, e, body)


def deploy_result_page(nid, token):
    return ("<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>部署进度 · 强哥公益雷达</title><style>%s</style></head><body>"
            "<div class=card><h1>正在部署<em>DEPLOYING</em></h1>"
            "<p class=sub>通常 30~90 秒，请勿关闭页面。</p>"
            "<div id=kv></div><div class=log id=log>连接中…</div>"
            "<p class=tip>部署完成后请把站点地址和后台密码保存好。</p></div>"
            "<script>(function(){var id=%s,tk=%s,last=0,fin=0;"
            "function poll(){fetch('/api/deploy/status?id='+encodeURIComponent(id)+'&t='+encodeURIComponent(tk))"
            ".then(r=>r.json()).then(function(d){"
            "if(!d.ok){document.getElementById('log').textContent='任务不存在或已被清理';return;}"
            "var lg=document.getElementById('log');"
            "lg.textContent=(d.log||[]).join('\\n');lg.scrollTop=lg.scrollHeight;"
            "var kv=document.getElementById('kv');"
            "if(d.status==='ok'){kv.innerHTML='<div class=kv><b>站点地址</b><span>http://'+d.host+':'+(d.web_port||8080)+'/</span></div>'"
            "+'<div class=kv><b>后台密码</b><span>'+d.admin_pass+'</span></div>'"
            "+'<div class=kv><b>授权码</b><span>'+d.license+'</span></div>'"
            "+'<div class=kv><b>到期日</b><span>'+d.expire+'</span></div>';fin=1;}"
            "else if(d.status==='warn'||d.status==='fail'){fin=1;}"
            "if(!fin)setTimeout(poll,2000);}).catch(function(){setTimeout(poll,3000);});}"
            "setTimeout(poll,1200);})();</script></body></html>"
            ) % (_DEPLOY_CSS, json.dumps(nid), json.dumps(token))


def _new_node(f, host, pw):
    nid = "n" + time.strftime("%y%m%d%H%M%S") + "%02d" % random.randint(0, 99)
    code = gen_license_code()
    expire = (f.get("expire") or "").strip()
    if not expire:
        expire = time.strftime("%Y-%m-%d", time.localtime(time.time() + 30 * 86400))
    d = license_load()
    d["codes"].insert(0, {
        "code": code,
        "note": ((" · ".join([x for x in [(f.get("note") or "").strip(), host] if x])))[:60],
        "host": "", "expire": expire, "enabled": True,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "last_used": "", "used_count": 0,
    })
    license_save(d)
    apw = "Kv" + "".join(random.choice(CODE_ALPHABET) for _ in range(8))
    rec = {
        "id": nid, "host": host,
        "ssh_port": int(f.get("ssh_port") or 22) if str(f.get("ssh_port") or "22").isdigit() else 22,
        "ssh_user": (f.get("ssh_user") or "root").strip() or "root",
        "ssh_pass": pw,
        "site_name": (f.get("site_name") or CONFIG.get("site_name") or "强哥公益雷达").strip()[:40],
        "dir": (f.get("dir") or "/www/wwwroot/king-vision").strip() or "/www/wwwroot/king-vision",
        # 端口锁定：APP 客户端写死 8888；网页独立走 WEB_PORT
        "web_port": WEB_PORT,
        "ws_port": 8888,
        "tcp_port": 9999,
        "unit": "kv-" + nid, "admin_pass": apw, "license": code, "expire": expire,
        "note": (f.get("note") or "").strip()[:120], "status": "pending",
        "cfg": {
            "popup_on": True,
            "popup_repeat_days": int(CONFIG.get("popup_repeat_days") or 1),
            "popup_eyebrow": CONFIG.get("popup_eyebrow"),
            "popup_title": CONFIG.get("popup_title"),
            "popup_desc": CONFIG.get("popup_desc"),
            "popup_tip": CONFIG.get("popup_tip"),
            "popup_group": CONFIG.get("popup_group"),
        },
        "token": "".join(random.choice(CODE_ALPHABET) for _ in range(10)),
        "created": int(time.time()), "updated": int(time.time()),
        "log_list": [], "log": "",
    }
    items = load_nodes()
    items.append(rec)
    save_nodes()
    return rec


async def handle_public_deploy(writer, method, body, ip):
    if method == "GET":
        send_html(writer, 200, deploy_page())
        return
    now = time.time()
    hits = [t for t in DEPLOY_HITS.get(ip, []) if now - t < 3600]
    if len(hits) >= 3:
        send_html(writer, 429, deploy_page(err="提交太频繁，请一小时后再试"))
        return
    f = parse_form(body)
    host = (f.get("host") or "").strip()
    pw = (f.get("ssh_pass") or "").strip()
    if not host or not pw:
        send_html(writer, 200, deploy_page(err="服务器 IP 与 SSH 密码必填"))
        return
    _dirv = (f.get("dir") or "/www/wwwroot/king-vision").strip()
    if (not _dirv.startswith("/")) or (":" in _dirv) or ("\\" in _dirv):
        send_html(writer, 200, deploy_page(
            err="安装目录必须是 Linux 绝对路径，例如 /www/wwwroot/king-vision", d=f))
        return
    # 网页独立 WEB_PORT；8888/9999 留给游戏数据（APP 客户端锁死）
    _webp = int(WEB_PORT)
    _datap = 8888
    _user = (f.get("ssh_user") or "root").strip() or "root"
    _port = int(f.get("ssh_port") or 22) if str(f.get("ssh_port") or "22").isdigit() else 22
    # 提交前先探一次端口：被占就让用户自己换，不静默改
    try:
        _cli = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _ssh_connect(host, _port, _user, pw, 10))
        try:
            _busy, _ours = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _port_state(_cli, _datap))
            _wbusy, _wours = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _port_state(_cli, _webp))
        finally:
            try:
                _cli.close()
            except Exception:
                pass
    except Exception as _ex:
        send_html(writer, 200, deploy_page(
            err="连不上服务器：%s（请检查 IP / 端口 / 密码）" % repr(_ex)[:90], d=f))
        return
    if _busy and not _ours:
        send_html(writer, 200, deploy_page(
            err="目标机 %d 端口已被别的程序占用，请先停掉再部署（数据端口固定 8888，与 APP 客户端一致）" % _datap, d=f))
        return
    if _wbusy and not _wours:
        send_html(writer, 200, deploy_page(
            err="目标机 %d 网页端口已被别的程序占用，请先停掉再部署" % _webp, d=f))
        return
    _code_err, _code_rec = check_deploy_code(f.get("deploy_code") or "")
    if _code_err:
        send_html(writer, 200, deploy_page(err=_code_err, d=f))
        return
    hits.append(now)
    DEPLOY_HITS[ip] = hits
    rec = _new_node(f, host, pw)
    rec["deploy_code"] = (f.get("deploy_code") or "").strip().upper()
    save_nodes()
    import threading as _th
    _th.Thread(target=deploy_node, args=(rec["id"],), daemon=True).start()
    log("自助部署 %s → %s@%s" % (rec["id"], rec["ssh_user"], host))
    send_html(writer, 200, deploy_result_page(rec["id"], rec["token"]))


def send_html(writer, code, html, extra=""):
    http_response(writer, code, html, "text/html; charset=utf-8", extra)


def redirect(writer, url):
    send_html(writer, 302, "", "Location: %s\r\n" % url)


def unauthorized_page(reason):
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>未授权 · 强哥公益雷达</title>' + ADMIN_STYLE +
        '</head><body><div class="login"><div class="card">'
        '<h2>授权已到期</h2><p class="sub">RENEW REQUIRED</p>'
        '<div class="msg err">本实例的授权已失效，续期后即可恢复使用。</div>'
        '<p class="tip">请联系管理员续期：</p>'
        '<p class="tip" style="font-size:15px;color:#e6c260;letter-spacing:1px">'
        'QQ 群 ' + esc(str(CONFIG.get("popup_group") or "")) + '　（备注「强哥公益雷达」）</p>'
        '<p class="tip muted">续期后刷新本页即可。</p>'
        '</div></div></body></html>')


def admin_handle(writer, method, path, headers, body):
    """返回 True 表示已处理，False 交回静态文件。"""
    p = path.split("?")[0]
    q = {}
    if "?" in path:
        q = dict(urllib.parse.parse_qsl(path.split("?", 1)[1], keep_blank_values=True))
    tok = get_cookie(headers, "kv_admin")
    authed = session_ok(tok)
    hit = p in ("/admin", "/admin/", "/admin/index.html")

    if p == "/admin/login" and method == "POST":
        f = parse_form(body)
        if f.get("password", "") == str(CONFIG["admin_pass"]):
            t = new_session("admin")
            send_html(writer, 302, "",
                      "Location: /admin\r\nSet-Cookie: kv_admin=%s; Path=/; HttpOnly; Max-Age=%d\r\n"
                      % (t, SESSION_TTL))
        else:
            send_html(writer, 401, admin_login_html("密码不正确"))
        return True

    if p == "/admin/logout":
        SESSIONS.pop(tok, None)
        send_html(writer, 302, "",
                  "Location: /admin\r\nSet-Cookie: kv_admin=; Path=/; Max-Age=0\r\n")
        return True

    if hit and not authed:
        send_html(writer, 200, admin_login_html())
        return True

    if not authed:
        if p.startswith("/admin/") and p not in ("/admin/login", "/admin/logout"):
            redirect(writer, "/admin")
            return True
        return False

    if hit:
        send_html(writer, 200, admin_dash_html(q.get("msg", ""), q.get("err", "")))
        return True

    if p == "/admin/site" and method == "POST":
        f = parse_form(body)
        sn = (f.get("site_name") or "").strip()
        if sn:
            CONFIG["site_name"] = sn[:40]
        pw = (f.get("admin_pass") or "").strip()
        if pw:
            if len(pw) < 5:
                redirect(writer, "/admin?err=" + urllib.parse.quote("管理密码至少 5 位"))
                return True
            CONFIG["admin_pass"] = pw
        if "default_source" in f:
            CONFIG["default_source"] = (f.get("default_source") or "").strip()[:120]
        if "public_url" in f:
            pu = (f.get("public_url") or "").strip()[:160]
            if pu and not pu.startswith("http"):
                pu = "http://" + pu
            if pu and not pu.endswith("/"):
                pu += "/"
            CONFIG["public_url"] = pu
        CONFIG["announce_title"] = (f.get("announce_title") or "").strip()[:60] or "系统公告"
        CONFIG["announce_content"] = (f.get("announce_content") or "").strip()[:4000]
        CONFIG["announce_mode"] = "banner" if (f.get("announce_mode") == "banner") else "modal"
        try:
            CONFIG["announce_repeat"] = max(0, min(10080, int(f.get("announce_repeat") or 120)))
        except Exception:
            CONFIG["announce_repeat"] = 120
        CONFIG["announce_on"] = (f.get("announce_on") == "1")
        CONFIG["announce_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_config()
        redirect(writer, "/admin?msg=" + urllib.parse.quote("设置已保存"))
        return True

    if p == "/admin/popup" and method == "POST":
        f = parse_form(body)
        CONFIG["popup_on"] = (f.get("popup_on") == "1")
        try:
            CONFIG["popup_repeat_days"] = max(0, min(365, int(f.get("popup_repeat_days") or 1)))
        except Exception:
            CONFIG["popup_repeat_days"] = 1
        g = re.sub(r"[^0-9]", "", f.get("popup_group") or "")
        if g:
            CONFIG["popup_group"] = g[:16]
        CONFIG["popup_eyebrow"] = (f.get("popup_eyebrow") or "")[:80]
        CONFIG["popup_title"] = (f.get("popup_title") or "")[:200]
        CONFIG["popup_desc"] = (f.get("popup_desc") or "")[:400]
        CONFIG["popup_tip"] = (f.get("popup_tip") or "")[:120]
        save_config()
        redirect(writer, "/admin?msg=" + urllib.parse.quote("弹窗设置已保存") + "#popup")
        return True

    if p == "/admin/deploy" and method == "POST":
        f = parse_form(body)
        host = (f.get("host") or "").strip()
        pw = (f.get("ssh_pass") or "").strip()
        mode = (f.get("mode") or "ssh").strip()
        if mode == "local":
            host = host or (local_ip() or "127.0.0.1")
            pw = "-"
        elif not host or not pw:
            redirect(writer, "/admin?err=" + urllib.parse.quote("服务器 IP 与 SSH 密码必填") + "#deploy")
            return True
        nid = "n" + time.strftime("%y%m%d%H%M%S") + "%02d" % random.randint(0, 99)
        code = gen_license_code()
        expire = (f.get("expire") or "").strip()
        if not expire:
            # 默认一个月，静默到期（子站到期后才显示授权失效页）
            expire = time.strftime("%Y-%m-%d", time.localtime(time.time() + 30 * 86400))
        d = license_load()
        d["codes"].insert(0, {
            "code": code,
            "note": ((" · ".join([x for x in [(f.get("note") or "").strip(), host] if x])))[:60],
            "host": "",
            "expire": expire,
            "enabled": True,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_used": "",
            "used_count": 0,
        })
        license_save(d)
        apw = (f.get("admin_pass") or "").strip()
        if not apw:
            apw = "Kv" + "".join(random.choice(CODE_ALPHABET) for _ in range(8))
        rec = {
            "id": nid,
            "host": host,
            "ssh_port": int(f.get("ssh_port") or 22) if str(f.get("ssh_port") or "22").isdigit() else 22,
            "ssh_user": (f.get("ssh_user") or "root").strip() or "root",
            "ssh_pass": pw,
            "site_name": (f.get("site_name") or CONFIG.get("site_name") or "强哥公益雷达").strip()[:40],
            "dir": (f.get("dir") or "/www/wwwroot/king-vision").strip() or "/www/wwwroot/king-vision",
            # 端口锁定：APP 客户端写死 8888；网页独立走 WEB_PORT
            "web_port": WEB_PORT,
            "ws_port": 8888,
            "tcp_port": 9999,
            "unit": "kv-" + nid,
            "admin_pass": apw,
            "license": code,
            "expire": expire,
            "note": (f.get("note") or "").strip()[:120],
            "status": "pending",
            "cfg": {
                "popup_on": True,
                "popup_repeat_days": int(CONFIG.get("popup_repeat_days") or 1),
                "popup_eyebrow": CONFIG.get("popup_eyebrow"),
                "popup_title": CONFIG.get("popup_title"),
                "popup_desc": CONFIG.get("popup_desc"),
                "popup_tip": CONFIG.get("popup_tip"),
                "popup_group": CONFIG.get("popup_group"),
            },
            "created": int(time.time()),
            "updated": int(time.time()),
            "log_list": [],
            "log": "",
        }
        items = load_nodes()
        items.append(rec)
        save_nodes()
        import threading as _th
        _target = deploy_local if mode == "local" else deploy_node
        _th.Thread(target=_target, args=(nid,), daemon=True).start()
        redirect(writer, "/admin?msg=" + urllib.parse.quote("已开始部署 " + host + "，日志见「部署记录」") + "#nodes")
        return True

    if p == "/admin/node/action" and method == "POST":
        f = parse_form(body)
        nid = (f.get("id") or "").strip()
        act = (f.get("action") or "").strip()
        rec = get_node(nid)
        if not rec:
            redirect(writer, "/admin?err=" + urllib.parse.quote("节点不存在") + "#nodes")
            return True
        if act == "push_global":
            _sync_popup_cfg(rec)
            save_nodes()
            _m = node_action(rec, "push")
            redirect(writer, "/admin?msg=" + urllib.parse.quote(
                "%s：%s" % (rec.get("host"), _m)) + "#nodes")
            return True
        if act == "push_all":
            _msgs = []
            for _it in load_nodes():
                _sync_popup_cfg(_it)
                _msgs.append("%s %s" % (_it.get("host"), node_action(_it, "push")))
            save_nodes()
            redirect(writer, "/admin?msg=" + urllib.parse.quote(
                ("全部推送完成：" + " | ".join(_msgs))[:220]) + "#nodes")
            return True
        if act in ("lic_off", "lic_on"):
            _code = str(rec.get("license") or "").strip().upper()
            _hit = False
            _d = license_load()
            for _it in _d.get("codes", []):
                if str(_it.get("code", "")).strip().upper() == _code:
                    _it["enabled"] = (act == "lic_on")
                    _hit = True
                    break
            if _hit:
                license_save(_d)
                redirect(writer, "/admin?msg=" + urllib.parse.quote(
                    "已%s该站点授权（%s），2 分钟内生效" % ("恢复" if act == "lic_on" else "停用", _code)) + "#nodes")
            else:
                redirect(writer, "/admin?err=" + urllib.parse.quote("没找到该节点的授权码") + "#nodes")
            return True
        if act == "del":
            save_nodes([x for x in load_nodes() if x.get("id") != nid])
            redirect(writer, "/admin?msg=" + urllib.parse.quote("已删除记录（服务器未动）") + "#nodes")
            return True
        if act == "push":
            rec["expire"] = (f.get("expire") or "").strip()[:20]
            cf = rec.setdefault("cfg", {})
            cf["popup_on"] = (f.get("popup_on") == "1")
            try:
                cf["popup_repeat_days"] = max(0, min(365, int(f.get("popup_repeat_days") or 1)))
            except Exception:
                cf["popup_repeat_days"] = 1
            cf["popup_title"] = (f.get("popup_title") or "")[:200]
            cf["popup_desc"] = (f.get("popup_desc") or "")[:400]
            g2 = re.sub(r"[^0-9]", "", f.get("popup_group") or "")
            if g2:
                cf["popup_group"] = g2[:16]
            save_nodes()
            # 同步授权码到期日
            if rec.get("license"):
                ld = license_load()
                for x in ld["codes"]:
                    if str(x.get("code", "")).upper() == str(rec["license"]).upper():
                        x["expire"] = rec["expire"]
                license_save(ld)
            msg = node_action(rec, "push", f)
            _say(rec, msg)
            redirect(writer, "/admin?msg=" + urllib.parse.quote(msg) + "#nodes")
            return True
        if act == "remove":
            msg = node_action(rec, "remove", f)
            rec["status"] = "stopped"
            _say(rec, msg)
            redirect(writer, "/admin?msg=" + urllib.parse.quote(msg) + "#nodes")
            return True
        msg = node_action(rec, act, f)
        if act == "stop":
            rec["status"] = "stopped"
        elif act == "start":
            rec["status"] = "ok"
        _say(rec, msg)
        redirect(writer, "/admin?msg=" + urllib.parse.quote(msg) + "#nodes")
        return True

    if p.startswith("/admin/license/"):
        f = parse_form(body) if method == "POST" else {}
        d = license_load()
        code = (f.get("code") or "").strip().upper()
        if p == "/admin/license/add":
            if not code:
                code = gen_license_code()
            if any(str(x.get("code", "")).strip().upper() == code for x in d["codes"]):
                redirect(writer, "/admin?err=" + urllib.parse.quote("授权码已存在"))
                return True
            d["codes"].insert(0, {
                "code": code,
                "note": (f.get("note") or "").strip(),
                "host": (f.get("host") or "").strip(),
                "expire": (f.get("expire") or "").strip(),
                "kind": ("deploy" if (f.get("kind") == "deploy") else "site"),
                "enabled": True,
                "max_uses": 0,
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_used": "",
                "used_count": 0,
            })
            license_save(d)
            redirect(writer, "/admin?msg=" + urllib.parse.quote("已新增授权码 " + code))
            return True
        if p == "/admin/license/batch":
            try:
                _cnt = max(1, min(200, int(f.get("count") or 10)))
            except Exception:
                _cnt = 10
            _kind = "deploy" if f.get("kind") == "deploy" else "site"
            try:
                _days = int(f.get("days") or 30)
            except Exception:
                _days = 30
            _exp = "" if _days <= 0 else time.strftime(
                "%Y-%m-%d", time.localtime(time.time() + _days * 86400))
            _pre = (f.get("note") or "").strip()[:40]
            _made = []
            for _i in range(_cnt):
                _c = gen_license_code()
                _guard = 0
                while any(str(x.get("code", "")).strip().upper() == _c for x in d["codes"]) and _guard < 50:
                    _c = gen_license_code()
                    _guard += 1
                d["codes"].insert(0, {
                    "code": _c,
                    "note": (_pre + (" #%d" % (_i + 1) if _pre else "")).strip(),
                    "host": "",
                    "expire": _exp,
                    "kind": _kind,
                    "enabled": True,
                    "max_uses": 0,
                    "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_used": "",
                    "used_count": 0,
                })
                _made.append(_c)
            license_save(d)
            redirect(writer, "/admin?msg=" + urllib.parse.quote(
                "已批量生成 %d 个%s（%s）" % (
                    _cnt, "部署卡密" if _kind == "deploy" else "授权码",
                    ("有效期至 " + _exp) if _exp else "永久")) + "#license")
            return True
        if p == "/admin/license/del":
            before = len(d["codes"])
            d["codes"] = [x for x in d["codes"]
                          if str(x.get("code", "")).strip().upper() != code]
            license_save(d)
            redirect(writer, "/admin?msg=" + urllib.parse.quote(
                "已删除 %d 个授权码" % (before - len(d["codes"]))))
            return True
        if p == "/admin/license/toggle":
            for x in d["codes"]:
                if str(x.get("code", "")).strip().upper() == code:
                    x["enabled"] = not x.get("enabled", True)
            license_save(d)
            redirect(writer, "/admin?msg=" + urllib.parse.quote("已更新状态"))
            return True
        if p == "/admin/license/expire":
            for x in d["codes"]:
                if str(x.get("code", "")).strip().upper() == code:
                    x["expire"] = (f.get("expire") or "").strip()[:20]
            license_save(d)
            redirect(writer, "/admin?msg=" + urllib.parse.quote("授权到期已更新") + "#license")
            return True
        redirect(writer, "/admin")
        return True

    if p.startswith("/admin"):
        # 后台未知路径直接 404，别回落到 dist/admin 里的旧静态页
        http_response(writer, 404, "not found")
        return True
    return False


async def handle_http(reader, writer, web=True):
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except Exception:
        return
    try:
        head = raw.decode("latin1")
        lines = head.split("\r\n")
        method, path, _ = lines[0].split(" ", 2)
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        body = b""
        clen = 0
        try:
            clen = int(headers.get("content-length", "0") or 0)
        except Exception:
            clen = 0
        if clen > 0:
            if clen > (2 << 20):
                clen = 2 << 20
            body = await reader.readexactly(clen)
        p0 = path.split("?")[0]
        _is_ws = headers.get("upgrade", "").lower() == "websocket"
        # 数据端口（8888）只跑游戏数据：不做网页，老链接 302 到网页端口
        if not web and not _is_ws:
            try:
                _h = (headers.get("host") or "").rsplit(":", 1)[0] or "127.0.0.1"
            except Exception:
                _h = "127.0.0.1"
            _loc = "http://%s:%d%s" % (_h, WEB_PORT, path)
            writer.write(("HTTP/1.1 302 Found\r\nLocation: %s\r\n"
                          "Content-Length: 0\r\nConnection: close\r\n\r\n"
                          % _loc).encode())
            await writer.drain()
            return
        # 未授权：只放行授权校验接口（LICENSE_OFF=True 时永不触发）
        if (not LICENSE_OFF) and (not LICENSE_STATE["ok"]) and not p0.startswith("/api/license/"):
            send_html(writer, 403, unauthorized_page(LICENSE_STATE["reason"]))
            await writer.drain()
            return
        # 子站模式：后台 / 自助部署 / 后台 API 一律不对外
        if CONFIG.get("child_mode") and (
                p0.startswith("/admin") or p0 == "/deploy"
                or p0.startswith("/api/admin") or p0.startswith("/api/deploy")):
            http_response(writer, 404, "Not Found")
            await writer.drain()
            return
        if p0 == "/deploy":
            _ip = "0.0.0.0"
            try:
                _ip = writer.get_extra_info("peername")[0]
            except Exception:
                pass
            await handle_public_deploy(writer, method, body, _ip)
            await writer.drain()
            return
        if p0 == "/api/deploy/status":
            _q = urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "")
            _nid = (_q.get("id", [""])[0] or "").strip()
            _tk = (_q.get("t", [""])[0] or "").strip()
            _rec = get_node(_nid)
            if not _rec or (_rec.get("token") or "") != _tk:
                http_response(writer, 200, json.dumps({"ok": False}))
            else:
                http_response(writer, 200, json.dumps({
                    "ok": True, "status": _rec.get("status") or "pending",
                    "log": (_rec.get("log_list") or [])[-60:],
                    "host": _rec.get("host"), "ws_port": _rec.get("ws_port"),
                    "web_port": _rec.get("web_port") or WEB_PORT,
                    "admin_pass": _rec.get("admin_pass"), "license": _rec.get("license"),
                    "expire": _rec.get("expire"),
                }, ensure_ascii=False))
            await writer.drain()
            return
        if _is_ws:
            if p0 == "/proxy-ws":
                await handle_proxy_ws(reader, writer, headers,
                                      path.split("?", 1)[1] if "?" in path else "")
            else:
                await handle_ws(reader, writer, headers)
            return
        if p0 in ("/app.apk", "/app", "/download", "/download/app.apk", "/king.apk"):
            serve_app(writer)
            await writer.drain()
            return
        if p0.startswith("/hero/"):
            serve_hero(writer, p0[len("/hero/"):].split(".")[0])
            await writer.drain()
            return
        if p0.startswith("/skin/"):
            serve_skin(writer, p0[len("/skin/"):].split(".")[0])
            await writer.drain()
            return
        if p0.startswith("/admin"):
            if admin_handle(writer, method, path, headers, body):
                await writer.drain()
                return
        if p0.startswith("/api/"):
            api_response(writer, path, body, headers)
            await writer.drain()
            return
        if method not in ("GET", "HEAD"):
            http_response(writer, 404, "not found")
            await writer.drain()
            writer.close()
            return
        serve_static(writer, path)
        await writer.drain()
    except Exception as e:
        log("HTTP 异常:", repr(e))
    finally:
        try:
            writer.close()
        except Exception:
            pass


def parse_report(msg):
    """宽容解析上报行，返回 (room, target, payload) 或 None。

    已知的几种形态（均兼容）：
      gameData[==]<room>[==]<target>[==]<payload>
      gameData[==]<room>[==]==<payload>      (target 为空时粘连)
      gameData=<room>[==]<target>[==]<payload>
      gameData<room>[==]<target>[==]<payload>
      <room>[==]<target>[==]<payload>
    """
    s = msg.strip()
    if not s:
        return None
    if s.startswith("gameData"):
        s = s[8:]
    s = s.lstrip("=")
    if s.startswith("[==]"):
        s = s[4:]
    s = s.lstrip("=")
    if not s:
        return None
    parts = s.split("[==]")
    if len(parts) < 2:
        return None
    room = parts[0].strip().lstrip("=").strip()
    if not room or not all(ch.isalnum() or ch in "-_." for ch in room):
        return None
    if len(parts) == 2:
        target, payload = "", parts[1]
    else:
        target = parts[1].strip().lstrip("=").strip()
        payload = "[==]".join(parts[2:])
    payload = payload.strip()
    while payload.startswith("="):
        payload = payload[1:]
    payload = payload.strip()
    if not payload:
        return None
    return room, target, payload


def _iv(x, d=0):
    try:
        return int(float(str(x).strip()))
    except Exception:
        return d


def normalize_hero_seg(seg):
    """老版客户端英雄段 12 字段，1.2.8 前端要求 17 且第 10 位为阵营。
    这里补齐：复制阵营到 index 9，再补 0 到 17 位。"""
    f = seg.split(",")
    if len(f) >= 17 or len(f) < 10:
        return seg
    if _iv(f[9]) == 0 and _iv(f[8]) > 0:
        f = f[:9] + [f[8]] + f[9:]
    if len(f) < 17:
        f = f + ["0"] * (17 - len(f))
    return ",".join(f)


def normalize_payload(payload):
    """按分块结构做兼容，17 字段的 1.2.8 数据原样返回。"""
    if "==---" not in payload:
        return payload
    head, rest = payload.split("==---", 1)
    segs = head.split("==")
    if not segs or len(segs[0].split(",")) >= 17:
        return payload
    return "==".join(normalize_hero_seg(s) for s in segs) + "==---" + rest


def apply_report(msg):
    """解析上报数据，写入 ROOMS。返回是否成功。"""
    r = parse_report(msg)
    if not r:
        return False
    room, target, payload = r
    # 纯占位帧（"------" 之类，对局未开始/结束）：不建房间，避免空房间刷屏
    if not payload.strip("-= "):
        old = ROOMS.get(room)
        if old is None or not (old.get("data") or "").strip("-= "):
            ROOMS.pop(room, None)
        return False
    raw = payload
    payload = normalize_payload(payload)
    old = ROOMS.get(room)
    ROOMS[room] = {
        "data": payload,
        "ts": time.time(),
        "viewers": (old or {}).get("viewers", 1) or 1,
        "target": target,
    }
    if target:
        DMS[target] = {"data": payload, "ts": time.time()}
    prev_len = (old or {}).get("rawlen", 0) or 0
    if old is None or len(raw) != prev_len:
        log("上报房间 %s 负载 %d 字节 target=%r" % (room, len(raw), target))
    ROOMS[room]["rawlen"] = len(raw)
    now = time.time()
    if now - DUMP_TS.get(room, 0) > 10:
        DUMP_TS[room] = now
        dump_sample(room, raw)
    return True


def dump_sample(room, payload):
    """留样最近一帧，便于复盘客户端格式差异。"""
    try:
        d = os.path.join(DATA_DIR, "samples")
        os.makedirs(d, exist_ok=True)
        fn = re.sub(r"[^\w.-]", "_", str(room))[:40] + ".txt"
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            f.write(payload)
    except Exception:
        pass


def _handle_line(line):
    try:
        text = line.decode("utf-8", "ignore") if isinstance(line, bytes) else line
        text = text.strip()
        if not text:
            return
        if not apply_report(text) and _SAMPLE[0] < 3:
            _SAMPLE[0] += 1
            log("未识别的上报样本(%d字节): %r" % (len(text), text[:200]))
    except Exception as e:
        # 单帧异常不打断整条 TCP 连接
        log("单帧解析失败:", repr(e))


async def handle_tcp(reader, writer):
    addr = writer.get_extra_info("peername")
    log("TCP 上报连接:", addr)
    buf = b""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            STATS["up"] += len(chunk)
            buf += chunk
            # 1) 换行分隔（旧格式）
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                _handle_line(line)
            # 2) 无分隔符：真实客户端连发，按 gameData 起始标记切帧
            starts = [m.start() for m in re.finditer(b"gameData", buf)]
            if len(starts) >= 2:
                for i in range(len(starts) - 1):
                    _handle_line(buf[starts[i]:starts[i + 1]])
                buf = buf[starts[-1]:]
            elif starts and starts[0] > 0:
                buf = buf[starts[0]:]
            if len(buf) > (16 << 20):
                buf = buf[-(1 << 20):]
    except Exception as e:
        log("TCP 异常:", repr(e))
    finally:
        # 客户端断开时缓冲区里可能还留着最后一帧：够完整就补处理，半帧直接丢
        try:
            tail = buf.strip()
            if tail and (tail[-1:] == b"}" or re.fullmatch(rb"[0-9,\-=]+", tail)):
                _handle_line(tail)
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass


async def license_watch_loop():
    """部署端每 2 分钟复查一次授权，母站/免授权直接跳过。"""
    if LICENSE_OFF:
        return
    while True:
        await asyncio.sleep(120)
        if not str(CONFIG.get("license_server") or "").strip():
            continue
        ok, reason, exp = await asyncio.get_event_loop().run_in_executor(None, remote_license_check)
        old = LICENSE_STATE["ok"]
        LICENSE_STATE.update({"ok": ok, "reason": reason, "expire": exp})
        if ok != old:
            log("授权状态变化 -> %s %s" % ("ok" if ok else "fail", reason))


def ensure_admin_pass():
    """admin_pass 为空（源码包默认）时生成随机密码，只在日志里出现一次。"""
    if str(CONFIG.get("admin_pass") or "").strip():
        return
    pw = "".join(random.choice(CODE_ALPHABET) for _ in range(12))
    CONFIG["admin_pass"] = pw
    save_config()
    log("=" * 52)
    log("初始管理密码：%s" % pw)
    log("请登录 /admin 后到「站点设置」里修改")
    log("=" * 52)


def _spawn(coro):
    """兼容 Python 3.6：没有 asyncio.create_task 时退回 ensure_future。"""
    f = getattr(asyncio, "create_task", None) or asyncio.ensure_future
    return f(coro)


async def main():
    load_config()
    ensure_admin_pass()
    if (not LICENSE_OFF) and str(CONFIG.get("license_server") or "").strip():
        ok, reason, exp = await asyncio.get_event_loop().run_in_executor(None, remote_license_check)
        LICENSE_STATE.update({"ok": ok, "reason": reason, "expire": exp})
        log("授权校验 %s %s" % ("通过" if ok else "未通过", reason))
    if LICENSE_OFF:
        log("免授权模式（LICENSE_OFF=True）：不需要任何卡密/授权码")
    log("king-relay-lite 启动  web=:%d  data=:%d(tcp=:%d)  static=%s"
        % (WEB_PORT, WS_PORT, TCP_PORT, STATIC_DIR))
    # 网页/后台/部署页走 WEB_PORT；8888 只做游戏数据 WS，不提供网页
    if WEB_PORT == WS_PORT:
        web_srv = await asyncio.start_server(handle_http, "0.0.0.0", WEB_PORT, limit=1 << 20)
        data_srv = web_srv
    else:
        web_srv = await asyncio.start_server(handle_http, "0.0.0.0", WEB_PORT, limit=1 << 20)
        data_srv = await asyncio.start_server(
            lambda r, w: handle_http(r, w, False), "0.0.0.0", WS_PORT, limit=1 << 20)
    _spawn(ws_push_loop())
    _spawn(upstream_loop())
    _spawn(license_watch_loop())
    if LICENSE_OFF or LICENSE_STATE["ok"]:
        tcp_srv = await asyncio.start_server(handle_tcp, "0.0.0.0", TCP_PORT, limit=1 << 20)
        try:
            # 3.6 的 Server 没有 serve_forever（3.7+ 才有），start_server 后挂起即可
            await asyncio.Event().wait()
        finally:
            for _srv in (web_srv, data_srv, tcp_srv):
                try:
                    _srv.close()
                except Exception:
                    pass
    else:
        log("未授权，TCP 上报口不开放，仅提供授权提示页")
        try:
            await asyncio.Event().wait()
        finally:
            for _srv in (web_srv, data_srv):
                try:
                    _srv.close()
                except Exception:
                    pass


if __name__ == "__main__":
    try:
        if hasattr(asyncio, 'run'):
            asyncio.run(main())
        else:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
