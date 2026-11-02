#!/usr/bin/env python3
"""Build merged server address lists for classic multiplayer games.

Sources:
  - SupportedGames catalog (4841 games)
  - lookout-games: Masters across QuakeWorld, Quake 2, Quake 3, GameSpy 1, Unreal 2
  - OpenSpy: All games and active masters on openspy.net
  - Kingpin community masters + Hambloch endpoints

Outputs:
  - servers.txt: Unified list of all unique servers across all games
  - server.txt: Symlink / alias to servers.txt
  - servers/<game>.txt: Per-game server list for all 4841 supported games
  - servers.json: Index and statistics for all 4841 games
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import select
import socket
import struct
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SERVERS_DIR = ROOT_DIR / "servers"
DATA_DIR = ROOT_DIR / "data"
SUPPORTED_GAMES_JSON = DATA_DIR / "SupportedGames.json"
LOOKOUT_METADATA_JSON = DATA_DIR / "lookout_metadata.json"
LOOKOUT_LICENSE_FILE = DATA_DIR / "LICENSE"
LOOKOUT_INDEX_URL = "https://raw.githubusercontent.com/molycode/lookout-games/main/index.json"
LOOKOUT_GAME_URL_TMPL = "https://raw.githubusercontent.com/molycode/lookout-games/main/games/{game}/game.json"
LOOKOUT_LICENSE_URL = "https://raw.githubusercontent.com/molycode/lookout-games/main/LICENSE"
ALL_SERVERS_TXT = ROOT_DIR / "servers.txt"
ALL_SERVERS_SYMLINK = ROOT_DIR / "server.txt"
INDEX_JSON = ROOT_DIR / "servers.json"

OPENSPY_SERVERS_URL = "https://openspy-website.nyc3.digitaloceanspaces.com/servers.json"
OPENSPY_MASTER_HOST = "master.openspy.net"
OPENSPY_MASTER_PORT = 28900
NETWORKS333_MASTER_HOST = "master.333networks.com"
NETWORKS333_MASTER_PORT = 28900
GAMESPY_KEY = "mgNUaC"

ADDR_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})\b")
GAMESPY_IP_RE = re.compile(rb"\\ip\\(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})")

# Game name aliases (e.g. OpenSpy gamename -> lookout game key)
OPENSPY_ALIASES: dict[str, str] = {
    "ut": "ut99",
    "bfield1942": "bf1942",
}

# Aliases mapping variant game IDs to lookout definitions and masters
LOOKOUT_ALIASES: dict[str, str] = {
    # Quake 3 engine
    "q3a": "quake3",
    "quake3arena": "quake3",
    "callofduty": "cod",
    "enemyterritory": "et",
    "wolfet": "et",
    "wolf": "rtcw",
    # Quake 2 engine
    "q2": "quake2",
    # QuakeWorld engine
    "qw": "quakeworld",
    # Unreal engine
    "ut": "ut99",
    "tacticalops": "ut99",
    # Battlefield
    "bfield1942": "bf1942",
    # Medal of Honor
    "mohaab": "mohaa",
    "mohaas": "mohaa",
}

# Games with active retro multiplayer communities to query proactively
KNOWN_ACTIVE_GAMES: set[str] = {
    "swat4", "vietcong", "stella", "postal2", "deusex", "heretic2", "blood2",
    "rune", "kingpin", "fear", "fearxp2", "nolf", "nolf2", "painkiller",
    "painkillerod", "chaser", "chaserd", "globalops", "undying", "flatout2zp",
    "contractjack", "bballarenaps3", "bcommander", "bfvietnam", "capricorn",
    "coj2", "conflictsopc", "dh2004", "dh2005", "dtr2", "gore", "gorese",
    "jbnightfire", "legendsmm", "mclub3ps2", "nfs6", "projectigi2r", "rethawed",
    "rmth2003", "serioussamse", "srow2pc", "swrcommando", "terminator3",
    "thps4pc", "thugpro", "tribesv", "vietcong2", "vietnamso", "nerfarena",
    "soldieroffortune", "sof", "sof2", "avp2", "halo", "shogo", "daikatana",
}


def safe_filename(name: str) -> str:
    """Encode characters illegal on Windows/POSIX filesystems: / \\ : * ? " < > |."""
    res = []
    for ch in name:
        if ch in "/\\:*?\"<>|":
            res.append(f"%{ord(ch):02X}")
        else:
            res.append(ch)
    return "".join(res)


def _gs_charshift(reg: int) -> int:
    if reg < 26:
        return reg + 65
    if reg < 52:
        return reg + 71
    if reg < 62:
        return reg - 4
    if reg == 62:
        return 43
    if reg == 63:
        return 47
    return 0


def gamespy_validate(cipher: str, secure: str, enctype: int = 0) -> str:
    """GameSpy secure/validate (gsmsalg / enctype 0)."""
    cip = [ord(c) for c in cipher]
    sec = [ord(c) for c in secure]
    if not (0 < len(sec) <= 8 and 0 < len(cip) <= 8):
        return ""
    enc = list(range(256))
    j = 0
    for i in range(256):
        j = (j + enc[i] + cip[i % len(cip)]) % 256
        enc[j], enc[i] = enc[i], enc[j]
    tmp: list[int] = []
    j = 0
    k = 0
    for i in range(len(sec)):
        j = (j + sec[i] + 1) % 256
        left = enc[j]
        k = (k + left) % 256
        right = enc[k]
        enc[k] = left
        enc[j] = right
        tmp.append(sec[i] ^ enc[(left + right) & 0xFF])
    while len(tmp) % 3:
        tmp.append(0)
    if enctype == 2:
        for i in range(len(tmp)):
            tmp[i] ^= cip[i % len(cip)]
    val: list[int] = []
    for i in range(0, len(tmp), 3):
        left, mid, right = tmp[i], tmp[i + 1] % 256, tmp[i + 2] % 256
        val.append(_gs_charshift(left >> 2))
        val.append(_gs_charshift(((left & 3) << 4) | (mid >> 4)))
        val.append(_gs_charshift(((mid & 15) << 2) | (right >> 6)))
        val.append(_gs_charshift(right & 63))
    return "".join(chr(c) for c in val)


def _tcp_connect_ipv4(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    last_exc: Exception | None = None
    for family, socktype, proto, _, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            sock.close()
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"no IPv4 address for {host}")


def _recv_tcp(sock: socket.socket, first: float, more: float) -> bytes:
    chunks: list[bytes] = []
    sock.settimeout(first)
    try:
        while True:
            data = sock.recv(65535)
            if not data:
                break
            chunks.append(data)
            sock.settimeout(more)
    except socket.timeout:
        pass
    return b"".join(chunks)


def _read_gamespy_challenge(
    sock: socket.socket, wait: float = 6.0, nudge_first: bool = False
) -> bytes:
    if nudge_first:
        try:
            sock.sendall(b"\n")
        except OSError as exc:
            raise RuntimeError(f"connection dead before challenge: {exc}") from exc

    ready, _, _ = select.select([sock], [], [], wait)
    if ready:
        chal = _recv_tcp(sock, 1.5, 0.3)
        if b"\\secure\\" in chal:
            return chal
        if not chal:
            return b""

    if nudge_first:
        return b""

    try:
        sock.sendall(b"\n")
    except OSError as exc:
        raise RuntimeError(f"connection dead before challenge: {exc}") from exc

    ready, _, _ = select.select([sock], [], [], wait)
    if not ready:
        return b""
    return _recv_tcp(sock, 1.5, 0.3)


def fetch_gamespy1_master(
    host: str,
    port: int,
    master_game: str,
    query_port_offset: int = 0,
    cipher_key: str = GAMESPY_KEY,
    timeout: float = 6.0,
    attempts: int = 3,
) -> list[str]:
    """Fetch server list via GameSpy 1 master protocol."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        sock: socket.socket | None = None
        try:
            sock = _tcp_connect_ipv4(host, port, timeout)
            chal = _read_gamespy_challenge(sock, wait=5.0, nudge_first=(attempt % 2 == 1))
            if b"\\secure\\" not in chal:
                raise RuntimeError(f"no secure challenge: {chal[:60]!r}")
            secure = chal.split(b"\\secure\\", 1)[1].split(b"\\", 1)[0].decode("latin-1", errors="replace")
            # For gspylite master handshake, the client key is always GAMESPY_KEY ("mgNUaC")
            validate = gamespy_validate(GAMESPY_KEY, secure, 0)
            if not validate and cipher_key and cipher_key != GAMESPY_KEY:
                validate = gamespy_validate(cipher_key, secure, 0)
            if not validate:
                raise RuntimeError("empty validate token")

            req = (
                f"\\gamename\\gspylite\\location\\0\\validate\\{validate}"
                f"\\final\\\\list\\\\gamename\\{master_game}\\final\\"
            ).encode("latin-1", errors="replace")
            sock.sendall(req)

            body = _recv_tcp(sock, 4.0, 1.0)
            if not body:
                raise RuntimeError("empty list response")

            addrs: list[str] = []
            for ip_b, port_b in GAMESPY_IP_RE.findall(body):
                ip = ip_b.decode("latin-1", errors="replace")
                qport = int(port_b)
                gport = qport - query_port_offset
                if 1 <= gport <= 65535:
                    addrs.append(f"{ip}:{gport}")
            return list(dict.fromkeys(addrs))
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
        finally:
            if sock:
                sock.close()
    assert last_exc is not None
    raise last_exc


def parse_q2_packed_servers(data: bytes, query_port_offset: int = 0) -> list[str]:
    addrs: list[str] = []
    if data.startswith(b"\xff\xff\xff\xff"):
        data = data[4:]
    idx = data.find(b"servers")
    if idx < 0:
        return addrs
    payload = data[idx + len(b"servers") :]
    while payload.startswith(b"\n") or payload.startswith(b" "):
        payload = payload[1:]

    for i in range(0, len(payload) - 5, 6):
        chunk = payload[i : i + 6]
        if chunk == b"\x00" * 6:
            break
        ip = ".".join(str(b) for b in chunk[:4])
        (qport,) = struct.unpack("!H", chunk[4:6])
        gport = qport - query_port_offset
        if 1 <= gport <= 65535:
            addrs.append(f"{ip}:{gport}")
    return addrs


def fetch_q2_master(
    host: str, port: int = 27900, query_port_offset: int = 0, timeout: float = 4.0
) -> list[str]:
    """Fetch servers from Quake 2 UDP master."""
    payload = b"\xff\xff\xff\xffquery"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, (host, port))
        chunks: list[bytes] = []
        while True:
            try:
                data, _ = sock.recvfrom(65535)
                chunks.append(data)
                sock.settimeout(0.4)
            except socket.timeout:
                break
        if not chunks:
            raise RuntimeError("no response from master")
        addrs: list[str] = []
        for c in chunks:
            addrs.extend(parse_q2_packed_servers(c, query_port_offset))
        return list(dict.fromkeys(addrs))
    finally:
        sock.close()


def fetch_qw_master(
    host: str, port: int = 27000, query_port_offset: int = 0, timeout: float = 4.0
) -> list[str]:
    """Fetch servers from QuakeWorld master (c\\n -> \\xff\\xff\\xff\\xffd\\n)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"c\n", (host, port))
        chunks: list[bytes] = []
        while True:
            try:
                data, _ = sock.recvfrom(65535)
                chunks.append(data)
                sock.settimeout(0.4)
            except socket.timeout:
                break
        if not chunks:
            raise RuntimeError("no response from master")
        addrs: list[str] = []
        for c in chunks:
            if c.startswith(b"\xff\xff\xff\xffd\n"):
                payload = c[6:]
                for i in range(0, len(payload) - 5, 6):
                    chunk = payload[i : i + 6]
                    if chunk == b"\x00" * 6:
                        break
                    ip = ".".join(str(b) for b in chunk[:4])
                    (qport,) = struct.unpack("!H", chunk[4:6])
                    gport = qport - query_port_offset
                    if 1 <= gport <= 65535:
                        addrs.append(f"{ip}:{gport}")
        return list(dict.fromkeys(addrs))
    finally:
        sock.close()


def parse_q3_datagram(
    data: bytes, is_hex: bool = False, query_port_offset: int = 0
) -> list[str]:
    prefix = b"\xff\xff\xff\xffgetserversResponse"
    if not data.startswith(prefix):
        return []
    pos = len(prefix)
    while pos < len(data) and data[pos] in (10, 0, 32):
        pos += 1
    addrs: list[str] = []
    end_markers = (b"\\EOT", b"\\EOF")
    while pos < len(data):
        if data[pos : pos + 1] == b"\\":
            if any(data[pos : pos + 4] == m for m in end_markers):
                break
            if pos + 1 == len(data):
                break
            pos += 1
        if is_hex:
            if pos + 12 > len(data):
                break
            hex_str = data[pos : pos + 12].decode("ascii", "ignore")
            pos += 12
            if len(hex_str) == 12:
                try:
                    ip = ".".join(
                        str(int(hex_str[i : i + 2], 16)) for i in (0, 2, 4, 6)
                    )
                    qport = int(hex_str[8:12], 16)
                    gport = qport - query_port_offset
                    if 1 <= gport <= 65535:
                        addrs.append(f"{ip}:{gport}")
                except ValueError:
                    pass
        else:
            if pos + 6 > len(data):
                break
            chunk = data[pos : pos + 6]
            pos += 6
            ip = ".".join(str(b) for b in chunk[:4])
            (qport,) = struct.unpack("!H", chunk[4:6])
            gport = qport - query_port_offset
            if 1 <= gport <= 65535:
                addrs.append(f"{ip}:{gport}")
    return addrs


def fetch_q3_master(
    host: str,
    port: int,
    query_str: str,
    is_hex: bool = False,
    query_port_offset: int = 0,
    timeout: float = 4.0,
) -> list[str]:
    """Fetch servers from Quake 3 master protocol."""
    queries: list[str] = []
    if "," in query_str:
        parts = query_str.split(" ")
        versions = parts[0].split(",")
        rest = " ".join(parts[1:])
        for v in versions:
            queries.append(f"{v} {rest}")
    else:
        queries.append(query_str)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        chunks: list[bytes] = []
        for q in queries:
            req = f"\xff\xff\xff\xffgetservers {q}".encode("latin1")
            sock.sendto(req, (host, port))
            sock.settimeout(timeout)
            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    chunks.append(data)
                    sock.settimeout(0.4)
                except socket.timeout:
                    break
        if not chunks:
            raise RuntimeError("no response from master")
        addrs: list[str] = []
        for c in chunks:
            addrs.extend(parse_q3_datagram(c, is_hex, query_port_offset))
        return list(dict.fromkeys(addrs))
    finally:
        sock.close()


def fetch_unreal2_master(
    host: str,
    port: int = 28902,
    query_port_offset: int = 0,
    timeout: float = 6.0,
) -> list[str]:
    """Fetch servers from Unreal Engine 2 master (UT2004)."""
    sock = _tcp_connect_ipv4(host, port, timeout)

    def recv_exact(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise EOFError("socket closed prematurely")
            buf += chunk
        return buf

    def recv_frame() -> bytes:
        lb = recv_exact(4)
        (flen,) = struct.unpack("<I", lb)
        return recv_exact(flen)

    def make_frame(data: bytes) -> bytes:
        return struct.pack("<I", len(data)) + data

    def make_string(s: str) -> bytes:
        b = s.encode("latin1")
        return bytes([len(b) + 1]) + b + b"\0"

    try:
        # 1. Challenge
        recv_frame()

        # 2. Client response
        key_hash = "0" * 32
        client_resp = make_frame(
            make_string(key_hash)
            + make_string(key_hash)
            + make_string("UT2K4CLIENT")
            + struct.pack("<IB", 3369, 0)
            + make_string("int")
            + struct.pack("<IIIB", 0, 0, 0, 0)
        )
        sock.sendall(client_resp)

        # 3. Approved
        recv_frame()

        # 4. Verification
        verif = make_frame(make_string(key_hash))
        sock.sendall(verif)

        # 5. Verified
        recv_frame()

        # 6. List request
        list_req = make_frame(b"\0\0")
        sock.sendall(list_req)

        # 7. Count frame
        count_frame = recv_frame()
        (count,) = struct.unpack("<I", count_frame[:4])

        addrs: list[str] = []
        for _ in range(count):
            sf = recv_frame()
            if len(sf) >= 8:
                ip = ".".join(str(b) for b in sf[:4])
                port_le, _ = struct.unpack("<HH", sf[4:8])
                addrs.append(f"{ip}:{port_le}")
        return list(dict.fromkeys(addrs))
    finally:
        sock.close()


def fetch_http_urls(url: str, timeout: float = 15.0) -> list[str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "game-servers-mirror/1.0 (+https://github.com/shavargo)"
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")
    addrs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = ADDR_RE.match(line)
        if m:
            addrs.append(f"{m.group(1)}:{m.group(2)}")
    return addrs


def normalize_addr(addr: str) -> str | None:
    m = ADDR_RE.match(addr.strip())
    if not m:
        return None
    ip, port_s = m.group(1), int(m.group(2))
    if not (1 <= port_s <= 65535):
        return None
    octets = [int(x) for x in ip.split(".")]
    if any(o > 255 for o in octets):
        return None
    return f"{ip}:{port_s}"


def sort_key(addr: str) -> tuple[tuple[int, ...], int]:
    ip, port_s = addr.split(":")
    return (tuple(int(x) for x in ip.split(".")), int(port_s))


def load_cached_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    addrs: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        norm = normalize_addr(line)
        if norm:
            addrs.append(norm)
    return addrs


def discover_openspy_games() -> dict[str, dict]:
    """Fetch active game list from OpenSpy servers.json."""
    try:
        req = urllib.request.Request(
            OPENSPY_SERVERS_URL,
            headers={"User-Agent": "game-servers-mirror/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {item.get("gamename", "").strip(): item for item in data if item.get("gamename")}
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Warning: could not reach OpenSpy servers.json: {exc}", file=sys.stderr)
        return {}


def sync_lookout_metadata() -> dict[str, dict]:
    """Fetch and sync game definitions dynamically from molycode/lookout-games repo.

    Preserves licensing compliance and pulls master fixes, port offsets, and new games
    automatically. Gracefully falls back to local data/lookout_metadata.json if offline.
    """
    local_data: dict[str, dict] = {}
    if LOOKOUT_METADATA_JSON.is_file():
        try:
            local_data = json.loads(LOOKOUT_METADATA_JSON.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Warning reading local {LOOKOUT_METADATA_JSON}: {exc}", file=sys.stderr)

    print("[*] Syncing definitions from molycode/lookout-games...")
    try:
        req = urllib.request.Request(
            LOOKOUT_INDEX_URL,
            headers={"User-Agent": "retro-server-mirror/1.0 (+https://github.com/shavargo/retro-server-mirror)"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            idx = json.loads(resp.read().decode("utf-8"))

        games_dict = idx.get("games", {})
        if not games_dict:
            print("[!] lookout-games index returned empty catalog, using local snapshot.")
            return local_data

        def _fetch_one(gid: str) -> tuple[str, dict | None]:
            url = LOOKOUT_GAME_URL_TMPL.format(game=gid)
            try:
                g_req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "retro-server-mirror/1.0"},
                )
                with urllib.request.urlopen(g_req, timeout=8) as g_resp:
                    g_json = json.loads(g_resp.read().decode("utf-8"))
                    g_json["id"] = gid
                    return gid, g_json
            except Exception as e:  # noqa: BLE001
                print(f"[!] Could not fetch {gid} from lookout-games: {e}", file=sys.stderr)
                return gid, None

        fetched: dict[str, dict] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for gid, g_meta in ex.map(_fetch_one, games_dict.keys()):
                if g_meta is not None:
                    fetched[gid] = g_meta

        if fetched:
            # Sync MIT license from lookout-games alongside the data file
            try:
                lic_req = urllib.request.Request(
                    LOOKOUT_LICENSE_URL,
                    headers={"User-Agent": "retro-server-mirror/1.0"},
                )
                with urllib.request.urlopen(lic_req, timeout=5) as lic_resp:
                    LOOKOUT_LICENSE_FILE.write_bytes(lic_resp.read())
            except Exception as lic_err:  # noqa: BLE001
                print(f"[!] Note: could not refresh lookout LICENSE file: {lic_err}", file=sys.stderr)

            merged = dict(local_data)
            merged.update(fetched)
            LOOKOUT_METADATA_JSON.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
            print(f"[+] Synced {len(fetched)} game definitions from lookout-games (MIT, Copyright (c) 2026 moly)")
            return merged
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Could not sync live lookout-games definitions ({exc}), using cached snapshot.", file=sys.stderr)

    return local_data


def load_all_catalogs() -> tuple[list[dict], dict[str, dict]]:
    """Load SupportedGames.json (4841 games) and build lookup dictionary."""
    if not SUPPORTED_GAMES_JSON.is_file():
        raise FileNotFoundError(f"Missing {SUPPORTED_GAMES_JSON}")
    catalog: list[dict] = json.loads(SUPPORTED_GAMES_JSON.read_text(encoding="utf-8"))

    # Sync and load Lookout metadata
    lookout_map = sync_lookout_metadata()

    # Map by gamename lowercase
    games_by_name: dict[str, dict] = {}
    for item in catalog:
        gn = item.get("gamename", "").strip()
        if not gn:
            continue
        g_def = {
            "gamename": gn,
            "name": item.get("label", gn),
            "cipher": item.get("cipher", ""),
            "port": item.get("port", 0),
            "protocol": item.get("protocol", "gamespy1"),
            "category": item.get("category", "GameSpy PC Classic"),
            "queryPortOffset": 0,
            "masters": [],
            "q2_masters": [],
            "http_sources": [],
        }

        # Check lookout enrichment with aliases
        lower_gn = gn.lower()
        lk_target = LOOKOUT_ALIASES.get(lower_gn, lower_gn)
        lookout_meta = lookout_map.get(lk_target)
        if not lookout_meta:
            for lk_key, lk_val in lookout_map.items():
                mg = lk_val.get("protocolOptions", {}).get("masterGame", "").lower()
                if mg in (lower_gn, lk_target) or lk_key.lower() in (lower_gn, lk_target):
                    lookout_meta = lk_val
                    break

        if lookout_meta:
            if not g_def["name"] or g_def["name"] == gn:
                g_def["name"] = lookout_meta.get("name", g_def["name"])
            g_def["protocol"] = lookout_meta.get("protocol", g_def["protocol"])
            g_def["protocolOptions"] = dict(lookout_meta.get("protocolOptions", {}))
            g_def["queryPortOffset"] = lookout_meta.get("queryPortOffset", 0)
            g_def["masters"] = list(lookout_meta.get("masters", []))
            if "q2_masters" in lookout_meta:
                g_def["q2_masters"] = list(lookout_meta["q2_masters"])
            if "http_sources" in lookout_meta:
                g_def["http_sources"] = list(lookout_meta["http_sources"])

        # Special Kingpin enrichment
        if lower_gn == "kingpin":
            extra_gs = [
                {"host": "master.errorist.eu", "port": 28900},
                {"host": "master.333networks.com", "port": 28900},
                {"host": "master.frag-net.com", "port": 28900},
                {"host": "master.openspy.net", "port": 28900},
            ]
            for em in extra_gs:
                if not any(m["host"] == em["host"] for m in g_def["masters"]):
                    g_def["masters"].append(em)
            g_def["q2_masters"] = [
                {"host": "master.hambloch.com", "port": 27900},
                {"host": "master.shavargo.com", "port": 27900},
            ]
            g_def["http_sources"] = [
                "https://hambloch.com/kingpin/servers_active.php?gameport=1"
            ]

        games_by_name[gn] = g_def

    return catalog, games_by_name


def fetch_game_servers(
    game_id: str, game_def: dict, servers_dir: Path
) -> tuple[str, list[str], dict[str, int], list[str]]:
    """Query all masters for one game and merge with local cache."""
    sources: dict[str, list[str]] = {}
    errors: list[str] = []

    # 1. Cache
    fname = safe_filename(game_id)
    cache_path = servers_dir / f"{fname}.txt"
    cached = load_cached_file(cache_path)
    if cached:
        sources["cache"] = cached

    proto = game_def.get("protocol", "gamespy1")
    opts = game_def.get("protocolOptions", {})
    q_offset = game_def.get("queryPortOffset", 0)

    # 2. HTTP sources (e.g. Kingpin Hambloch)
    for url in game_def.get("http_sources", []):
        label = f"http://{url.split('://', 1)[-1]}"
        try:
            addrs = fetch_http_urls(url)
            sources[label] = addrs
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")

    # 3. Quake 2 UDP masters (e.g. Kingpin native masters)
    for m in game_def.get("q2_masters", []):
        host, port = m["host"], m.get("port", 27900)
        label = f"q2udp://{host}:{port}"
        try:
            addrs = fetch_q2_master(host, port, q_offset)
            sources[label] = addrs
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")

    # 4. Standard masters from game definition
    masters = game_def.get("masters", [])
    for m in masters:
        host, port = m["host"], m.get("port")
        label = f"{proto}://{host}:{port}"
        try:
            if proto in ("gamespy1", "unreal", "gamespy2"):
                master_game = opts.get("masterGame", game_id)
                addrs = fetch_gamespy1_master(
                    host, port or 28900, master_game, q_offset, cipher_key=GAMESPY_KEY
                )
            elif proto in ("quake2", "quake1"):
                addrs = fetch_q2_master(host, port or 27900, q_offset)
            elif proto == "quakeworld":
                addrs = fetch_qw_master(host, port or 27000, q_offset)
            elif proto in ("quake3", "quake4", "doom3", "tribes"):
                q_str = opts.get("masterQuery", "68 empty full")
                is_hex = opts.get("masterEntries") == "hex"
                addrs = fetch_q3_master(host, port or 27950, q_str, is_hex, q_offset)
            elif proto == "unreal2":
                addrs = fetch_unreal2_master(host, port or 28902, q_offset)
            else:
                continue
            sources[label] = addrs
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")

    # Merge: live sources take priority over cache
    merged: dict[str, str] = {}
    for label, addrs in sources.items():
        if label == "cache":
            continue
        for a in addrs:
            norm = normalize_addr(a)
            if norm and norm not in merged:
                merged[norm] = label

    # Retain cache entries
    if "cache" in sources:
        for a in sources["cache"]:
            norm = normalize_addr(a)
            if norm and norm not in merged:
                merged[norm] = "cache"

    source_counts = {lbl: len(addrs) for lbl, addrs in sources.items()}
    sorted_addrs = sorted(merged.keys(), key=sort_key)
    return game_id, sorted_addrs, source_counts, errors


def write_game_file(
    game_id: str,
    game_name: str,
    game_def: dict,
    addrs: list[str],
    source_counts: dict[str, int],
    errors: list[str],
    out_path: Path,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    category = game_def.get("category", "")
    proto = game_def.get("protocol", "gamespy1")
    default_port = game_def.get("port", 0)

    lines = [
        f"# Server address list for {game_name} ({game_id})",
        f"# Last fetch (UTC): {now}",
        f"# Category: {category} | Protocol: {proto} | Default Port: {default_port}",
        f"# Active servers found: {len(addrs)}",
    ]
    if source_counts:
        lines.append("# Sources:")
        for lbl, count in source_counts.items():
            lines.append(f"#   - {lbl}: {count} found")
    if errors:
        lines.append("# Master connection issues (cached entries preserved):")
        for err in errors:
            lines.append(f"#   - {err}")
    lines.append("# Format: ip:port")
    lines.append("#")
    lines.extend(addrs)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Loading game catalogs...")
    catalog, games_by_name = load_all_catalogs()
    print(f"Loaded {len(catalog)} games in SupportedGames catalog.")

    # Check live active games on OpenSpy
    print("Checking active games on OpenSpy...")
    openspy_active = discover_openspy_games()
    print(f"OpenSpy reports {len(openspy_active)} active titles.")

    # Determine which games have active masters / servers to query
    to_query: set[str] = set()

    for item in catalog:
        gn = item["gamename"]
        g_def = games_by_name.get(gn, {})
        lower_gn = gn.lower()

        # 1. Has explicit masters from lookout or kingpin or aliases
        if g_def.get("masters") or g_def.get("q2_masters") or g_def.get("http_sources"):
            # Ensure GameSpy 1 / Unreal games also query 333networks and OpenSpy
            if g_def.get("protocol") in ("gamespy1", "unreal"):
                if not any(m.get("host") == OPENSPY_MASTER_HOST for m in g_def.get("masters", [])):
                    g_def["masters"].append({"host": OPENSPY_MASTER_HOST, "port": OPENSPY_MASTER_PORT})
                if not any(m.get("host") == NETWORKS333_MASTER_HOST for m in g_def.get("masters", [])):
                    g_def["masters"].append({"host": NETWORKS333_MASTER_HOST, "port": NETWORKS333_MASTER_PORT})
            to_query.add(gn)
            continue

        # 2. Known active community games or reported active on OpenSpy
        if (
            lower_gn in openspy_active
            or lower_gn in KNOWN_ACTIVE_GAMES
            or OPENSPY_ALIASES.get(lower_gn) in openspy_active
            or LOOKOUT_ALIASES.get(lower_gn) in games_by_name
        ):
            if not any(m.get("host") == OPENSPY_MASTER_HOST for m in g_def.get("masters", [])):
                g_def["masters"].append({"host": OPENSPY_MASTER_HOST, "port": OPENSPY_MASTER_PORT})
            if not any(m.get("host") == NETWORKS333_MASTER_HOST for m in g_def.get("masters", [])):
                g_def["masters"].append({"host": NETWORKS333_MASTER_HOST, "port": NETWORKS333_MASTER_PORT})
            to_query.add(gn)
            continue

        # 3. Has existing non-empty cache file
        fname = safe_filename(gn)
        cache_path = SERVERS_DIR / f"{fname}.txt"
        if cache_path.is_file():
            cached = load_cached_file(cache_path)
            if cached:
                if not any(m.get("host") == OPENSPY_MASTER_HOST for m in g_def.get("masters", [])):
                    g_def["masters"].append({"host": OPENSPY_MASTER_HOST, "port": OPENSPY_MASTER_PORT})
                if not any(m.get("host") == NETWORKS333_MASTER_HOST for m in g_def.get("masters", [])):
                    g_def["masters"].append({"host": NETWORKS333_MASTER_HOST, "port": NETWORKS333_MASTER_PORT})
                to_query.add(gn)
                continue

    # Also include OpenSpy aliases (e.g. bfield1942, ut)
    for os_gn in openspy_active:
        target = OPENSPY_ALIASES.get(os_gn, os_gn)
        if target in games_by_name:
            g_def = games_by_name[target]
            if not any(m.get("host") == OPENSPY_MASTER_HOST for m in g_def.get("masters", [])):
                g_def["masters"].append({"host": OPENSPY_MASTER_HOST, "port": OPENSPY_MASTER_PORT})
            if not any(m.get("host") == NETWORKS333_MASTER_HOST for m in g_def.get("masters", [])):
                g_def["masters"].append({"host": NETWORKS333_MASTER_HOST, "port": NETWORKS333_MASTER_PORT})
            to_query.add(target)

    print(f"Actively querying upstream masters for {len(to_query)} games with known servers...")

    all_servers: set[str] = set()
    game_results: dict[str, tuple[list[str], dict[str, int], list[str]]] = {}
    start_time = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {
            executor.submit(fetch_game_servers, gn, games_by_name[gn], SERVERS_DIR): gn
            for gn in to_query
        }
        for future in concurrent.futures.as_completed(futures):
            gn = futures[future]
            try:
                gid, addrs, source_counts, errors = future.result()
                game_results[gid] = (addrs, source_counts, errors)
                all_servers.update(addrs)
                gname = games_by_name[gid]["name"]
                if addrs:
                    status_str = f"{len(addrs)} servers"
                    if errors:
                        status_str += f" ({len(errors)} master errors)"
                    print(f"  [{gid:15}] {gname[:30]:30} -> {status_str}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [{gn:15}] ERROR: {exc}", file=sys.stderr)

    # Now write server files only for active games with servers (>0)
    print(f"\nWriting individual server lists for games with active servers...")
    manifest_games: dict[str, dict] = {}
    active_games_written = 0

    for item in catalog:
        gn = item["gamename"]
        g_def = games_by_name[gn]
        gname = g_def["name"]
        fname = safe_filename(gn)
        out_path = SERVERS_DIR / f"{fname}.txt"

        if gn in game_results:
            addrs, source_counts, errors = game_results[gn]
        else:
            cached = load_cached_file(out_path)
            addrs = cached
            source_counts = {"cache": len(cached)} if cached else {}
            errors = []
            if cached:
                all_servers.update(cached)

        if addrs:
            write_game_file(gn, gname, g_def, addrs, source_counts, errors, out_path)
            active_games_written += 1
            manifest_games[gn] = {
                "name": gname,
                "category": g_def.get("category", "GameSpy PC Classic"),
                "protocol": g_def.get("protocol", "gamespy1"),
                "server_count": len(addrs),
                "file": f"servers/{fname}.txt",
            }
        else:
            if out_path.is_file() or out_path.is_symlink():
                out_path.unlink()
            manifest_games[gn] = {
                "name": gname,
                "category": g_def.get("category", "GameSpy PC Classic"),
                "protocol": g_def.get("protocol", "gamespy1"),
                "server_count": 0,
                "file": None,
            }

    # Clean up any leftover empty .txt files in SERVERS_DIR
    for p in SERVERS_DIR.glob("*.txt"):
        if p.is_file():
            lines = [
                line.strip()
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if not lines:
                p.unlink()

    # Write unified servers.txt at root
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sorted_all = sorted(all_servers, key=sort_key)
    all_lines = [
        "# Merged multiplayer server address list across all games",
        f"# Last fetch (UTC): {now}",
        f"# Total unique servers: {len(sorted_all)}",
        f"# Total games supported: {len(catalog)}",
        "# Sources: Lookout-games, OpenSpy, 333networks, Kingpin community masters",
        "#",
        "# Format: ip:port",
        "#",
    ]
    all_lines.extend(sorted_all)
    ALL_SERVERS_TXT.write_text("\n".join(all_lines) + "\n", encoding="utf-8")

    # Link / copy server.txt -> servers.txt
    if ALL_SERVERS_SYMLINK.is_symlink() or ALL_SERVERS_SYMLINK.exists():
        ALL_SERVERS_SYMLINK.unlink()
    try:
        ALL_SERVERS_SYMLINK.symlink_to("servers.txt")
    except OSError:
        ALL_SERVERS_SYMLINK.write_bytes(ALL_SERVERS_TXT.read_bytes())

    # Write servers.json manifest
    manifest = {
        "updated_at": now,
        "total_servers": len(sorted_all),
        "total_games": len(catalog),
        "active_games_count": active_games_written,
        "games": manifest_games,
    }
    INDEX_JSON.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    elapsed = time.time() - start_time
    print(
        f"\nDone! Fetched {len(sorted_all)} total unique servers across "
        f"{active_games_written} active games ({len(catalog)} supported) in {elapsed:.2f}s."
    )
    print(f"Master list written to {ALL_SERVERS_TXT}")
    print(f"Active game lists written to {SERVERS_DIR}/<game>.txt ({active_games_written} files)")
    print(f"Index written to {INDEX_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
