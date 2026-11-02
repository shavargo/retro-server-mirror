# retro-server-mirror

Regularly updated multi-game server address lists for launchers, server browsers, and community tools.

Pulls from master servers across **[molycode/lookout-games](https://github.com/molycode/lookout-games/)**, **[OpenSpy](https://openspy.net/server-list)**, the comprehensive **4,841-game SupportedGames catalog**, and game-specific community masters (e.g. Hambloch for Kingpin).

## Raw URLs

- **All Servers Combined (8,400+ servers):**  
  `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers.txt`  
  (also aliased as `server.txt`)

- **JSON Index / Metadata (All 4,841 Games):**  
  `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers.json`

- **Per-Game Server Lists:**  
  `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/<game>.txt`

  Examples:
  - Kingpin: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/kingpin.txt`
  - Unreal Tournament (UT99): `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/ut99.txt` (alias: `servers/ut.txt`)
  - Unreal Tournament 2004: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/ut2004.txt`
  - Quake III Arena: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/quake3.txt`
  - Quake II: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/quake2.txt`
  - QuakeWorld: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/quakeworld.txt`
  - Battlefield 1942: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/bf1942.txt` (alias: `servers/bfield1942.txt`)
  - Wolfenstein Enemy Territory: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/et.txt`
  - Call of Duty 4: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/cod4.txt`
  - Painkiller: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/painkiller.txt`
  - Rune: `https://raw.githubusercontent.com/shavargo/retro-server-mirror/main/servers/rune.txt`
  - ... and 4,830 more games!

## Supported Protocols & Sources

| Protocol | Masters / Sources | Description |
|----------|-------------------|-------------|
| **GameSpy 1 & 2** | `master.openspy.net:28900`, `master.333networks.com:28900`, `master.kingpin.info`, `master.errorist.eu`, `master.frag-net.com` | TCP master listing with challenge/validation (`gspylite` / `mgNUaC`). Covers UT99, Deus Ex, Rune, SiN, MOHAA, Kingpin, and all OpenSpy active titles. |
| **Quake 3** | `dpmaster.deathmask.net`, `master.quake3arena.com`, `master.ioquake3.org`, `etmaster.idsoftware.com`, `codmaster.activision.com`, etc. | UDP `getservers` protocol across id Tech 3 titles (Q3, CoD, CoD2, CoD4, ET, RTCW, JK2, JKA, Tremulous, Urban Terror, Warsow, Xonotic). Supports standard binary & hex entries (Elite Force). |
| **Quake 2** | `master.alienarena.org`, `master.quakeservers.net`, `master.maraakate.org`, `master.hambloch.com`, `master.shavargo.com` | UDP `query` on port 27900. Quake II, Alien Arena, Kingpin-native masters. |
| **QuakeWorld** | `master.quakeworld.nu:27000`, `master.quakeservers.net:27000`, `qwmaster.fodquake.net:27000` | UDP `c\n` query protocol. |
| **Unreal Engine 2** | `utmaster.openspy.net:28902`, `ut2004master.333networks.com:28902` | TCP framed handshake protocol for Unreal Tournament 2004. |
| **HTTP Endpoints** | Hambloch Kingpin API (`https://hambloch.com/kingpin/servers_active.php?gameport=1`) | Curated web endpoints for active game servers. |

## Port Handling & Caching

- **Game (Connect) Ports**: Addresses are game ports ready to pass to `+connect` or game launchers. For games where the master advertises query ports differing from join ports (such as Kingpin −10, BF1942 +8433, UT99 +1), the appropriate `queryPortOffset` is resolved.
- **Cache Retention**: Existing entries in `servers/<game>.txt` and `servers.txt` are merged back in on each run (no TTL), ensuring master server outages or network blips never drop known server addresses.

## Update Schedule

- **Automated Workflow**: GitHub Actions runs daily at 12:00 UTC sharp (`0 12 * * *`).
- **Manual Trigger**: Actions → **Update server lists** → **Run workflow**.

## Local Development

```bash
# Fetch and update all server lists across all 4,841 games
python3 scripts/fetch_servers.py
```

## Attribution & Acknowledgements

Server list data originates from community masters:
- [molycode/lookout-games](https://github.com/molycode/lookout-games/)
- [OpenSpy Master Server](https://openspy.net/)
- [333networks](https://333networks.com/)
- [M. Hambloch Kingpin Server List](https://hambloch.com/kingpin/)
- id Software / community master operators
