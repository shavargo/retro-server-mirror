#!/usr/bin/env python3
"""Build merged server address lists for classic multiplayer games.

Sources:
  - lookout-games: Masters across QuakeWorld, Quake 2, Quake 3, GameSpy 1, Unreal 2
  - OpenSpy: All games and active masters on openspy.net
  - Kingpin community masters + Hambloch endpoints

Outputs:
  - servers.txt: Unified list of all unique servers across all games
  - server.txt: Symlink / alias to servers.txt
  - servers/<game>.txt: Per-game server list
  - servers.json: Index and statistics for each game
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
LOOKOUT_GAMES_JSON = DATA_DIR / "lookout_games.json"
ALL_SERVERS_TXT = ROOT_DIR / "servers.txt"
ALL_SERVERS_SYMLINK = ROOT_DIR / "server.txt"
INDEX_JSON = ROOT_DIR / "servers.json"

OPENSPY_SERVERS_URL = "https://openspy-website.nyc3.digitaloceanspaces.com/servers.json"
OPENSPY_MASTER_HOST = "master.openspy.net"
OPENSPY_MASTER_PORT = 28900
GAMESPY_KEY = "mgNUaC"

ADDR_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})\b")
GAMESPY_IP_RE = re.compile(rb"\\ip\\(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})")

# Game name aliases (e.g. OpenSpy gamename -> lookout game key)
OPENSPY_ALIASES: dict[str, str] = {
    "ut": "ut99",
    "bfield1942": "bf1942",
}


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
            secure = chal.split(b"\\secure\\", 1)[1].split(b"\\", 1)[0].decode("ascii")
            validate = gamespy_validate(cipher_key, secure, 0)
            if not validate:
                raise RuntimeError("empty validate token")

            req = (
                f"\\gamename\\gspylite\\location\\0\\validate\\{validate}"
                f"\\final\\\\list\\\\gamename\\{master_game}\\final\\"
            ).encode("ascii")
            sock.sendall(req)

            body = _recv_tcp(sock, 4.0, 1.0)
            if not body:
                raise RuntimeError("empty list response")

            addrs: list[str] = []
            for ip_b, port_b in GAMESPY_IP_RE.findall(body):
                ip = ip_b.decode("ascii")
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
    while pos < len(data) and data[pos] in (10, 0, 32):  # \n, \0, space
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


def discover_openspy_games() -> list[dict]:
    """Fetch active game list from OpenSpy servers.json."""
    try:
        req = urllib.request.Request(
            OPENSPY_SERVERS_URL,
            headers={"User-Agent": "game-servers-mirror/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Warning: could not reach OpenSpy servers.json: {exc}", file=sys.stderr)
        return []


def load_all_game_definitions() -> dict[str, dict]:
    """Load Lookout games and merge OpenSpy games."""
    games: dict[str, dict] = {}
    if LOOKOUT_GAMES_JSON.is_file():
        games = json.loads(LOOKOUT_GAMES_JSON.read_text(encoding="utf-8"))

    # Pull active games from OpenSpy
    openspy_list = discover_openspy_games()
    for item in openspy_list:
        gn = item.get("gamename", "").strip()
        if not gn:
            continue
        target_key = OPENSPY_ALIASES.get(gn, gn)
        desc = item.get("description", target_key)

        if target_key in games:
            # Lookout game exists: ensure OpenSpy master is included
            g_def = games[target_key]
            masters = g_def.setdefault("masters", [])
            if not any(m.get("host") == OPENSPY_MASTER_HOST for m in masters):
                masters.append({"host": OPENSPY_MASTER_HOST, "port": OPENSPY_MASTER_PORT})
        else:
            # Standalone OpenSpy game
            games[target_key] = {
                "name": desc,
                "protocol": "gamespy1",
                "protocolOptions": {"masterGame": gn},
                "queryPortOffset": 0,
                "masters": [{"host": OPENSPY_MASTER_HOST, "port": OPENSPY_MASTER_PORT}],
            }

    return games


def fetch_game_servers(
    game_id: str, game_def: dict, servers_dir: Path
) -> tuple[str, list[str], dict[str, int], list[str]]:
    """Query all masters for one game and merge with local cache."""
    sources: dict[str, list[str]] = {}
    errors: list[str] = []

    # 1. Cache
    cache_path = servers_dir / f"{game_id}.txt"
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
            if proto == "gamespy1":
                master_game = opts.get("masterGame", game_id)
                addrs = fetch_gamespy1_master(
                    host, port or 28900, master_game, q_offset
                )
            elif proto == "quake2":
                addrs = fetch_q2_master(host, port or 27900, q_offset)
            elif proto == "quakeworld":
                addrs = fetch_qw_master(host, port or 27000, q_offset)
            elif proto == "quake3":
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
    addrs: list[str],
    source_counts: dict[str, int],
    errors: list[str],
    out_path: Path,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f"# Merged server address list for {game_name} ({game_id})",
        f"# Last fetch (UTC): {now}",
        "# Sources:",
    ]
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

    print("Loading game definitions...")
    games = load_all_game_definitions()
    print(f"Loaded {len(games)} games across Lookout and OpenSpy.")

    all_servers: set[str] = set()
    game_stats: dict[str, dict] = {}
    start_time = time.time()

    print(f"Querying masters concurrently across {len(games)} games...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        futures = {
            executor.submit(fetch_game_servers, gid, gdef, SERVERS_DIR): gid
            for gid, gdef in games.items()
        }
        for future in concurrent.futures.as_completed(futures):
            gid = futures[future]
            try:
                gid, addrs, source_counts, errors = future.result()
                gname = games[gid].get("name", gid)
                all_servers.update(addrs)

                # Write per-game file
                out_path = SERVERS_DIR / f"{gid}.txt"
                write_game_file(gid, gname, addrs, source_counts, errors, out_path)

                game_stats[gid] = {
                    "name": gname,
                    "servers": len(addrs),
                    "sources": source_counts,
                }
                status_str = f"{len(addrs)} servers"
                if errors:
                    status_str += f" ({len(errors)} master errors)"
                print(f"  [{gid:15}] {gname[:28]:28} -> {status_str}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [{gid:15}] ERROR: {exc}", file=sys.stderr)

    # Create alias symlinks for convenience
    alias_map = {
        "ut.txt": "ut99.txt",
        "bfield1942.txt": "bf1942.txt",
    }
    for alias_name, target_name in alias_map.items():
        alias_path = SERVERS_DIR / alias_name
        target_path = SERVERS_DIR / target_name
        if target_path.exists():
            if alias_path.is_symlink() or alias_path.exists():
                alias_path.unlink()
            try:
                alias_path.symlink_to(target_name)
            except OSError:
                alias_path.write_bytes(target_path.read_bytes())

    # Write unified servers.txt at root
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sorted_all = sorted(all_servers, key=sort_key)
    all_lines = [
        "# Merged multiplayer server address list across all games",
        f"# Last fetch (UTC): {now}",
        f"# Total unique servers: {len(sorted_all)}",
        f"# Total games: {len(game_stats)}",
        "# Sources: Lookout-games, OpenSpy, Kingpin community masters",
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
        "total_games": len(game_stats),
        "games": {
            k: {
                "name": v["name"],
                "server_count": v["servers"],
                "file": f"servers/{k}.txt",
            }
            for k, v in sorted(game_stats.items())
        },
    }
    INDEX_JSON.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    elapsed = time.time() - start_time
    print(
        f"\nDone! Fetched {len(sorted_all)} total unique servers across "
        f"{len(game_stats)} games in {elapsed:.2f}s."
    )
    print(f"Master list written to {ALL_SERVERS_TXT}")
    print(f"Game lists written to {SERVERS_DIR}/<game>.txt")
    print(f"Index written to {INDEX_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
