# M580 OPC UA Simulator

Serves the HMI variables of a Schneider Electric Control Expert program over OPC UA, with a
browser UI to view and change them. Two data sources:

| Mode | Tags and values come from | Needs a file? |
|---|---|---|
| **Export** (default) | A `.ZEF`/`.XEF` export. Values start at the program's initial values and change only when written; no logic runs. | Yes |
| **PLC** | A live PLC or Control Expert PLC simulator over **UMAS** (Modbus TCP 502). The PLC's data dictionary lists the variables; values are polled and writes go to the PLC. Started with **PLC** in the web UI, or at startup with `AUTOCONNECT=true`. | No. An optional `.ZEF`/`.XEF` adds comments and R/O / R/W. |

- **OPC UA:** `opc.tcp://<host>:4840` on all interfaces, anonymous, security policy None
- **Web UI:** `http://opcua-sim.localtest.me` (through Traefik)

## PLC mode

Use **PLC** in the web UI, or set `AUTOCONNECT: "true"` and `PLC_HOST` in `docker-compose.yaml`
to connect at startup. Without `AUTOCONNECT` the server starts in export mode and `PLC_HOST`
only pre-fills the PLC dialog. On connect the server:

1. Reads the PLC data dictionary: variable names, types, DDT member lists and memory offsets. The
   dictionary is cached in the data volume per application CRC, so later connects skip this (~14 s).
2. Expands structures, arrays and strings into leaf tags with the same names as the export parser,
   e.g. `HAC_RPP2_BRKR07.CntrlSel.ManCmd`.
3. Reads every tag once, then polls **on demand** (below).

### On-demand polling

The PLC answers one UMAS request per connection per PLC task cycle (MAST, 100 ms on the
reference project), whatever the request size. Reading everything every time would grow with
project size, so each `PLC_UPDATE_PERIOD` (1 s) the server:

1. Reads the **watched** tags: those an OPC UA client has subscribed to, read in the last 60 s,
   that are on screen in the web UI, or that were written in the last 10 s. Nearby tags share a
   byte-range reference, and up to ~100 references go in one request, so scattered tags are cheap.
   Their values are published as soon as they are read.
2. Spends the rest of the period reading the remaining memory in large chunks, round robin, so
   every other tag is still refreshed, about every few seconds; the PLC chip's tooltip shows how often.

Measured on the reference project against the Control Expert simulator, with no other clients:

| Watched tags | Requests per cycle | Cycle | Change in PLC → OPC UA subscriber |
|---|---|---|---|
| a handful | 1 | 1.0 s | median 0.5 s, max 1.1 s |
| 1,000 spread over the project | 11 | 1.2 s | |

Cost follows what clients watch, not project size. If a client subscribes to most of a large
project, the watched part alone needs more than one period (≈ watched data ÷ 927 bytes × task
cycle) and the cycle stretches accordingly. The tooltip shows the breakdown (subscribed / read /
web UI).

The PLC project must have **Project Settings › PLC embedded data › Data dictionary** enabled.
With "HMI variables only" set, the dictionary holds exactly the HMI-flagged variables.

**Writes.** Without a comments file every tag is writable, like a standard OPC UA server. An OPC
UA or web UI write goes to the PLC first; the node then takes the value read back from the PLC. If
the PLC rejects it or is unreachable, the client gets `BadCommunicationError` or `BadNotConnected`.
The PLC program may overwrite a written value on its next scan (e.g. a manual command while in Auto).

**Comments file (optional).** Upload a `.ZEF`/`.XEF` at any time. Tags are matched by path. Each
matched tag gets its comment as the OPC UA Description, and R/O / R/W is enforced from the comment
prefix. The nodes are updated in place, so client subscriptions continue. PLC tags not in the file
stay writable with no comment. **Remove file** in the UI makes everything writable again.

**Connection loss.** All nodes keep their last value with status `BadNotConnected`, writes are
refused, and the server reconnects with backoff. When the PLC returns with the same application,
the nodes and subscriptions are kept. If the application changed (new CRC), the dictionary is
re-read and the address space rebuilt.

The UMAS client (`app/umas.py`) is self-contained. It was written against the Apache PLC4X UMAS
message definitions, after testing showed plc4py's driver gives wrong offsets for nested DDT
members and is too slow for polling.

## How an export file is read

| Rule | Detail |
|---|---|
| Source | `.XEF` directly, or the `unitpro.xef` inside a `.ZEF` archive |
| Which variables | `<dataBlock>` variables with attribute `IsVariableHMI = -1` |
| Structures | DDTs, DFB public interfaces and arrays (incl. multi-dimensional) are expanded down to elementary tags, e.g. `HAC_RPP2_BRKR07.CntrlSel.ManCmd` |
| Comment | The member's comment from its DDT, overridden by any `instanceElementDesc` comment in an enclosing DDT or on the instance |
| Access | Comment starts with `R/O` → read only; `R/W` → read/write; no prefix → `DEFAULT_ACCESS` (R/W by default) |
| Initial value | DDT `variableInit`, overridden by instance `<value>`; IEC literals (`TRUE`, `16#FF`, `2#0101`, `t#1m30s`, `'text'`) |

R/O tags can't be written by OPC UA clients (`BadNotWritable`) or from the web UI. Client
writes are also checked against the PLC type: wrong type gives `BadTypeMismatch`, out of
range gives `BadOutOfRange`. Integers are accepted in any integer variant if they fit.

For the reference program `HAC_SKID_v00_01` this gives 1,806 HMI variables → 6,050 tags
(3,320 R/O, 1,738 R/W, 992 unprefixed I/O channels).

### Type mapping

| PLC | OPC UA | | PLC | OPC UA |
|---|---|---|---|---|
| BOOL, EBOOL | Boolean | | REAL / LREAL | Float / Double |
| INT / UINT | Int16 / UInt16 | | WORD / DWORD / BYTE | UInt16 / UInt32 / Byte |
| DINT / UDINT | Int32 / UInt32 | | TIME | UInt32 (ms) |
| STRING[n] | String (max n chars) | | DATE, TOD, DT | String |

### Address space

```
Objects
└─ HAC_SKID_v00_01                      (folder = project name)
   ├─ HAC_RPP2_BRKR07                   (folder, Description = variable comment)
   │  ├─ OpnCmd                         ns=2;s=HAC_RPP2_BRKR07.OpnCmd
   │  └─ CntrlSel
   │     └─ ManCmd                      ns=2;s=HAC_RPP2_BRKR07.CntrlSel.ManCmd
   └─ CPU_r0_s0_d01_c00m01_c0           ns=2;s=CPU_r0_s0_d01_c00m01_c0
```

Namespace URI `urn:m580-opcua-simulator:tags` (index 2). NodeIds are the Control Expert
variable paths. Each variable's `Description` attribute holds its comment.

When the tag set is reloaded (another file, switching between export and PLC mode, a new PLC
connection), nodes whose path and data type are unchanged are kept and updated in place, so
clients' subscriptions keep working. Removed tags report `BadNodeIdUnknown` to subscribers.

## Running with Docker and Traefik

`docker-compose.yaml` contains the simulator and, under the `traefik` profile, the Traefik
reverse proxy it needs. Both join the `proxy` network. Traefik routes:

| Route | Traefik entrypoint | Container port |
|---|---|---|
| HTTP `Host(opcua-sim.localtest.me)` | `web` (:80) | 8000 |
| TCP `HostSNI(*)` (raw OPC UA) | `opcua` (:4840) | 4840 |

**Packaged deployment** (new machine): builds the simulator and starts Traefik with it.

```bash
docker compose --profile traefik up -d --build
```

The Traefik dashboard is at http://traefik.localtest.me (or port 8080). Its Docker provider only
exposes containers labelled `traefik.enable=true`.

**Machine that already runs Traefik** (like this one, from
`/home/scada/Documents/docker-compose_traefik.yaml`): start only the simulator. The existing
Traefik must be on the `proxy` network and have the `opcua` entrypoint:

```yaml
    command:
      - "--entrypoints.opcua.address=:4840"
    ports:
      - "4840:4840"
```

```bash
docker compose up -d --build
```

Compose prints a warning that the `proxy` network was created by another project; it reuses it
and never removes it. Don't use `--profile traefik` there: the bundled container is also named
`traefik` and would clash.

Then open http://opcua-sim.localtest.me and drop a `.ZEF`/`.XEF` on the page. The last
file loaded is kept in the `opcua-sim-data` volume and reloaded on restart. In export mode
tag values start from the program's initial values after every restart or load.

Other containers on the `proxy` network (e.g. Ignition) can also connect directly to
`opc.tcp://opcua-sim:4840`. The server echoes the URL the client connected with in its
endpoint list, so clients work through the proxy without a hostname override.

`localtest.me` resolves to 127.0.0.1, so it only works on this machine. For the UI from
other machines, change the router rule to e.g.
``Host(`opcua-sim.localtest.me`) || Host(`192.168.0.142`)``.

### Settings (environment)

| Variable | Default | |
|---|---|---|
| `DEFAULT_ACCESS` | `RW` | Access for comments without an R/O or R/W prefix (`RW` or `RO`) |
| `OPCUA_PORT` | `4840` | |
| `OPCUA_SERVER_NAME` | `M580 OPC UA Simulator` | |
| `WEB_PORT` | `8000` | Inside the container |
| `MAX_UPLOAD_MB` | `100` | |
| `AUTOCONNECT` | `false` | `true`: connect to `PLC_HOST` at startup (PLC mode). Otherwise start in export mode |
| `PLC_HOST` | | PLC / PLC simulator address, used by `AUTOCONNECT` and to pre-fill the PLC dialog (the address last used in the UI is pre-filled instead, when there is one) |
| `PLC_PORT` | `502` | |
| `PLC_UNIT` | `1` | Modbus unit id for UMAS |
| `PLC_UPDATE_PERIOD` | `1.0` | Seconds between updates of watched tags (on-demand polling) |
| `PROGRAM_FILE` | | Export to load at start if none was uploaded yet (comments file in PLC mode) |
| `LOG_LEVEL` | `INFO` | |

## Web UI

- **PLC** button: connect to a PLC (host, port, unit id) or disconnect back to export mode. Shows link state, the number of watched tags and the update cycle; the tooltip shows the request breakdown and the background refresh time
- Load an export with **Load program** (export mode) or **Load comments file** (PLC mode), or by dropping the file on the page
- **Table / Tree** views. The tree mirrors the OPC UA address space: variables, then DDT members and array elements. Each folder shows its DDT type, structure comment and tag count. Expand all / Collapse all; arrow keys open and close a focused folder
- Click a column header to sort (again to reverse); default is Tag A→Z in natural order (`BRKR2` before `BRKR10`). In the tree, folders come first and items sort within each folder. Sorting by Value uses the values at the time of sorting; rows don't jump around as live values change
- Filter by any words in the tag path, comment, parent comments or parent DDT type; by R/W or R/O; by type; or show only tags changed from their initial value. In the tree, filtering opens the folders that contain matches
- Booleans are toggles; numbers and strings commit on Enter or when you leave the field (Esc cancels). Numeric fields accept IEC literals such as `16#FF`
- **Tag detail panel**: click a tag (or focus its name and press Enter). It shows:
  - the live value with an editor;
  - a trend of recent changes (hover or arrow keys for values) and a table of them with who made each change;
  - copyable NodeId and expanded NodeId, and the PLC → OPC UA data type;
  - the number of subscriptions, the structure chain with DDT types and comments, and the comment prefix;
  - the initial value (file mode) or PLC memory location (PLC mode).

  The server keeps the last 240 changes per tag in memory. In PLC mode an open panel counts as watching the tag
- **Snapshots**: save the current values of all tags, or of the tags the current filter shows, under a name. Apply one later to write back only writable tags whose value differs; R/O tags and tags missing from the program are skipped and reported. In PLC mode the snapshot's tags are read fresh from the PLC first, and changes are written in batches of up to ~70 per request. Snapshots can be downloaded and imported as JSON (`{"name": ..., "values": {"tag.path": value}}`) and are kept in the data volume
- **Diagnostics** (`#diagnostics`): OPC UA clients (application, address, subscriptions, monitored items, publishing interval); PLC polling (state, reconnects, MAST period, dictionary, watched/background requests, UMAS request rate and timing, cycle-time chart); server (mode, uptime, memory, versions). Refreshes every 2 s
- Values changed by the PLC, OPC UA clients or the UI appear live, with who changed them and when
- **Reset values** (export mode) puts every tag back to the program's initial value

API docs: http://opcua-sim.localtest.me/api/docs

## Tests

```bash
python3 -m unittest discover -s tests
```

End-to-end, export mode (reference program loaded, PLC disconnected):

```bash
docker run --rm --network proxy -v "$PWD/tests:/tests:ro" m580-opcua-sim:latest python /tests/e2e_opcua.py opc.tcp://opcua-sim:4840 http://opcua-sim:8000
```

End-to-end, PLC mode. This writes test values to the PLC simulator and restores them, checking
each one directly in PLC memory. `--relay` adds a simulated network outage:

```bash
docker run --rm --network host -v "$PWD/tests:/tests:ro" -v "$PWD/HAC_SKID_v00_01_003.ZEF:/ref.ZEF:ro" m580-opcua-sim:latest python /tests/e2e_plc.py opc.tcp://127.0.0.1:4840 http://opcua-sim.localtest.me 192.168.0.128 /ref.ZEF --relay 192.168.0.142:15020
```

## Limits

- No OPC UA security or user authentication. Use it on a trusted test network only.
- PLC mode writes to whatever PLC it is pointed at. Point it only at a simulator or a PLC you are allowed to change.
- PLC mode samples values: watched tags about every `PLC_UPDATE_PERIOD`, others every few seconds. Changes shorter than that are not seen. The first read of an unwatched tag returns its background value; it is fresh from the next cycle on.
- PLC comments are not stored in the data dictionary, so they need the optional export file.
- DATE, TOD and DT values are read-only in PLC mode.
- Export mode: values are not persisted across restarts, and no PLC logic runs.
- Arrays are exposed element by element, not as OPC UA array nodes.
- IODDT types aren't defined in the export, so variables of those types are skipped with a warning on load.
