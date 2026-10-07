# Zabbix template: Proxmox Backup Server by HTTP

**English** | [Русский](README.ru.md)

Zabbix template for monitoring **Proxmox Backup Server 4.x** through its REST API (HTTP agent).
No Zabbix agent and nothing to install on the PBS host.

- Zabbix: **7.4+** (uses nested low-level discovery)
- PBS: 4.x (tested against PBS 4.2 API responses)
- File: [`template_pbs_http.yaml`](template_pbs_http.yaml)

## What is monitored

| Area | Details |
|---|---|
| **Tasks** (last 24h, configurable) | OK / warnings / failed counters per category: backup, restore, sync, verify, prune, GC, tape, other. Text list of failed tasks with error messages. Trigger on failed backup / restore / other tasks |
| **Scheduled PBS jobs** (sync, verify, prune, garbage collection, tape backup) | Auto-discovered. Last run result (OK / warnings / error / running / never run), last and next run time, GC pending bytes. Triggers: last run failed, finished with warnings, has not run for too long |
| **Backup groups** (`vm/<id>`, `ct/<id>`, `host/<name>` in every datastore and namespace) | Age and time of the newest snapshot, snapshot count. Triggers when there is no new backup for 26h (warning) / 50h (high). Catches backup jobs that silently stopped running on the PVE side |
| **Datastores** | Total / used / available space, utilization %, PBS forecast of the "full" date, datastore errors (e.g. removable datastore not mounted) |
| **System** | CPU utilization, iowait, load average per CPU, memory, swap, root filesystem, uptime / restart, kernel, CPU model |
| **Disks** | SMART status, SSD wearout |
| **ZFS** | Pool health, size, utilization, fragmentation |
| **Other** | API availability, PBS version, subscription status, available package updates (trigger disabled by default), API certificate expiry |

Almost all triggers depend on `PBS: API is unavailable`, so a PBS outage produces one problem instead of dozens.

## How it works

Each API section is requested by a single HTTP agent "raw" item (no history). Dependent items extract
values from that JSON with JSONPath / JavaScript, so one request yields many metrics.

| Raw item | API endpoint | Interval |
|---|---|---|
| Get node status | `/nodes/localhost/status` | 1m |
| Get tasks | `/nodes/localhost/tasks` | 5m |
| Get datastore usage | `/status/datastore-usage` | 5m |
| Get sync / verify / prune / GC / tape job status | `/admin/sync`, `/admin/verify`, `/admin/prune`, `/admin/gc`, `/tape/backup` | 5m |
| Get disks | `/nodes/localhost/disks/list` | 1h |
| Get ZFS pools | `/nodes/localhost/disks/zfs` | 5m |
| Version, subscription, updates, certificate | `/version`, `/nodes/localhost/subscription`, `/nodes/localhost/apt/update`, `/nodes/localhost/certificates/info` | 1m–6h |

Backup groups use 3-level nested discovery:

```
datastores (from datastore-usage)
  └─ namespaces of each datastore      /admin/datastore/<store>/namespace   (1h)
       └─ backup groups of a namespace  /admin/datastore/<store>/groups      (1h)
            └─ per group: newest snapshot -> age -> triggers
```

Each backup group is polled with its own lightweight request every 15 minutes (`{$PBS.GROUP.INTERVAL}`),
because Zabbix does not allow dependent items across nested discovery levels.

> Backup jobs configured in **Proxmox VE** (Datacenter → Backup) are PVE objects and are not visible in
> the PBS API. They are covered indirectly: failed backup tasks and backup group freshness.

## Setup

### 1. PBS: create a read-only API token

```bash
proxmox-backup-manager user create zabbix@pbs
proxmox-backup-manager acl update / Audit --auth-id zabbix@pbs
proxmox-backup-manager user generate-token zabbix@pbs monitoring
proxmox-backup-manager acl update / Audit --auth-id 'zabbix@pbs!monitoring'
```

The `Audit` role is needed for **both** the user and the token (tokens have privilege separation).
Save the token secret printed by `generate-token`.

### 2. Zabbix

1. *Data collection → Templates → Import* → `template_pbs_http.yaml`.
2. Create a host, link the template **Proxmox Backup Server by HTTP**.
3. Add an interface of any type (e.g. Agent) with the PBS IP or DNS name. The URL is built from `{HOST.CONN}`.
4. Set macros on the host:

| Macro | Value |
|---|---|
| `{$PBS.TOKEN.ID}` | `zabbix@pbs!monitoring` (default) |
| `{$PBS.TOKEN.SECRET}` | token secret, type **Secret text** |

The TLS certificate is not verified, so the default self-signed PBS certificate works.

## Macros

| Macro | Default | Description |
|---|---|---|
| `{$PBS.SCHEME}` | `https` | API scheme |
| `{$PBS.PORT}` | `8007` | API port |
| `{$PBS.NODE}` | `localhost` | Node name in `/nodes/<node>/` paths |
| `{$PBS.TOKEN.ID}` | `zabbix@pbs!monitoring` | API token ID |
| `{$PBS.TOKEN.SECRET}` | | API token secret |
| `{$PBS.TIMEOUT}` | `15s` | HTTP timeout |
| `{$PBS.LLD.LIFETIME}` | `7d` | Keep lost resources for |
| `{$PBS.CPU.UTIL.CRIT}` | `90` | CPU utilization, % |
| `{$PBS.CPU.IOWAIT.MAX}` | `30` | iowait, % |
| `{$PBS.LOAD.PER.CPU.MAX}` | `1.5` | Load average per CPU |
| `{$PBS.MEMORY.PUSED.MAX}` | `90` | Memory utilization, % |
| `{$PBS.ROOTFS.PUSED.MAX}` | `90` | Root filesystem utilization, % |
| `{$PBS.CERT.EXPIRY.WARN}` | `14` | Certificate expiry warning, days |
| `{$PBS.TASKS.INTERVAL}` | `5m` | Task list polling interval |
| `{$PBS.TASKS.PERIOD}` | `24` | Task counters window, hours |
| `{$PBS.TASKS.LIMIT}` | `5000` | Max tasks requested (must cover the window) |
| `{$PBS.DATASTORE.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Datastore filter (regex) |
| `{$PBS.DATASTORE.PUSED.WARN}` | `80` | Datastore usage warning, %. Context: `{$PBS.DATASTORE.PUSED.WARN:"store1"}` |
| `{$PBS.DATASTORE.PUSED.CRIT}` | `90` | Datastore usage high, %. Supports context |
| `{$PBS.DATASTORE.FULL.DAYS}` | `30` | Warn if estimated full within N days. Supports context |
| `{$PBS.GROUP.INTERVAL}` | `15m` | Backup group polling interval |
| `{$PBS.GROUP.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Backup group filter, regex on `<store>/<ns>/<type>/<id>`, e.g. `store1/vm/100` |
| `{$PBS.GROUP.IGNORE.COMMENT}` | `^nomon` | Groups whose PBS comment matches get no freshness triggers |
| `{$PBS.GROUP.AGE.WARN}` | `26` | No new backup, warning, hours. Context: `{$PBS.GROUP.AGE.WARN:"vm/100"}` |
| `{$PBS.GROUP.AGE.CRIT}` | `50` | No new backup, high, hours. Supports context |
| `{$PBS.JOBS.INTERVAL}` | `5m` | Job status polling interval |
| `{$PBS.JOB.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Job filter (job ID; datastore name for GC) |
| `{$PBS.JOB.MAX.AGE}` | `48` | Scheduled job has not run for N hours. `verify` and `tape` contexts: `192` |
| `{$PBS.DISK.MATCHES}` / `NOT_MATCHES` | `.*` / `^(zd\|loop\|ram\|rbd)` | Disk filter |
| `{$PBS.DISK.WEAROUT.MAX}` | `80` | SSD wearout, % |
| `{$PBS.ZFS.PUSED.MAX}` | `80` | ZFS pool utilization, % |

## Tips

- **Weekly backups:** raise the age thresholds for a group, e.g. `{$PBS.GROUP.AGE.WARN:"vm/100"}` = `170`.
- **Deleted guests whose backups are kept:** put `nomon` at the beginning of the group comment in PBS,
  or exclude them with `{$PBS.GROUP.NOT_MATCHES}`.
- **Many backup groups:** increase `{$PBS.GROUP.INTERVAL}` to reduce the number of requests.
- An unmounted removable datastore shows an LLD error on its namespace discovery. This is expected;
  the problem is reported by the trigger `Datastore reports an error`.

## Credits

This template was created with the help of [Claude Code](https://claude.com/claude-code) (Anthropic).

## License

MIT
