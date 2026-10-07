# Zabbix template: Proxmox Backup Server by HTTP

**English** | [Русский](README.ru.md)

Zabbix template for monitoring **Proxmox Backup Server 4.x** through its REST API (HTTP agent).
No Zabbix agent and nothing to install on the PBS host.

- Zabbix: **7.4+** (uses nested low-level discovery)
- PBS: 4.x (tested against PBS 4.2 API responses)
- File: [`template_pbs_http.yaml`](template_pbs_http.yaml)

## What is monitored

The template is intentionally lightweight: only what is needed to know that backups are fine.
For a PBS with 2 datastores, ~25 backup groups and 6 jobs it creates about 45 items.

| Area | Details |
|---|---|
| **Backup groups** (`vm/<id>`, `ct/<id>`, `host/<name>` in every datastore and namespace) | Age of the newest snapshot. **Average** when there is no new backup for 26h, **High** after 50h. Catches backup jobs that silently stopped running or failed on the PVE side |
| **Failed backups** | Every backup task that fails on PBS creates a **High** problem within 5 minutes, e.g. `PBS: Backup failed: 2026-10-07 03:00 Backup-Lan:ct/118: <error>`. Resolves when there are no failed backups during the last 24h, or close it manually |
| **Scheduled PBS jobs** (sync, verify, prune, garbage collection) | Auto-discovered, named by datastore / namespace. Result of the last run. Triggers: last run failed (error text in the problem name), finished with warnings |
| **Datastores** | Space utilization % and available space. Triggers at 80% / 90% |
| **System** | CPU utilization, root filesystem usage |
| **API** | Availability. All other triggers depend on `PBS: API is unavailable`, so a PBS outage produces one problem instead of dozens |

## How it works

| Item | API endpoint | Interval |
|---|---|---|
| API availability | `/version` | 1m |
| Get node status (CPU, root FS) | `/nodes/localhost/status` | 1m |
| Failed backups | `/nodes/localhost/tasks?typefilter=backup&errors=1` | 5m |
| Get datastore usage | `/status/datastore-usage` | 5m |
| Get sync / verify / prune / GC job status | `/admin/sync`, `/admin/verify`, `/admin/prune`, `/admin/gc` | 5m |

"Get ..." items keep no history: dependent items take their values from that JSON.

Backup groups use 3-level nested discovery:

```
datastores (from datastore-usage)
  └─ namespaces of each datastore      /admin/datastore/<store>/namespace   (1h)
       └─ backup groups of a namespace  /admin/datastore/<store>/groups      (1h)
            └─ per group: one item "Last backup age" + 2 triggers
```

Each backup group is one HTTP item polled every 15 minutes (`{$PBS.GROUP.INTERVAL}`),
because Zabbix does not allow dependent items across nested discovery levels.

> Backup jobs configured in **Proxmox VE** (Datacenter → Backup) are PVE objects and are not visible in
> the PBS API. They are covered twice: a failed backup task alerts at once, and backup group freshness catches a job that did not run at all.

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
| `{$PBS.ROOTFS.PUSED.MAX}` | `90` | Root filesystem utilization, % |
| `{$PBS.TASKS.INTERVAL}` | `5m` | Failed backups polling interval |
| `{$PBS.TASKS.PERIOD}` | `24` | Failed backups are reported for N hours |
| `{$PBS.TASKS.LIMIT}` | `500` | Max failed backup tasks requested |
| `{$PBS.DATASTORE.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Datastore filter (regex) |
| `{$PBS.DATASTORE.PUSED.WARN}` | `80` | Datastore usage warning, %. Context: `{$PBS.DATASTORE.PUSED.WARN:"store1"}` |
| `{$PBS.DATASTORE.PUSED.CRIT}` | `90` | Datastore usage high, %. Supports context |
| `{$PBS.GROUP.INTERVAL}` | `15m` | Backup group polling interval |
| `{$PBS.GROUP.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Backup group filter, regex on `<store>/<ns>/<type>/<id>`, e.g. `store1/vm/100` |
| `{$PBS.GROUP.IGNORE.COMMENT}` | `^nomon` | Groups whose PBS comment matches get no freshness triggers |
| `{$PBS.GROUP.AGE.WARN}` | `26` | No new backup, average, hours. Context: `{$PBS.GROUP.AGE.WARN:"vm/100"}` |
| `{$PBS.GROUP.AGE.CRIT}` | `50` | No new backup, high, hours. Supports context |
| `{$PBS.JOBS.INTERVAL}` | `5m` | Job status polling interval |
| `{$PBS.JOB.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Job filter (job ID; datastore name for GC) |

## Tips

- **Weekly backups:** raise the age thresholds for a group, e.g. `{$PBS.GROUP.AGE.WARN:"vm/100"}` = `170`.
- **Deleted guests whose backups are kept:** put `nomon` at the beginning of the group comment in PBS,
  or exclude them with `{$PBS.GROUP.NOT_MATCHES}`.
- **Many backup groups:** increase `{$PBS.GROUP.INTERVAL}` to reduce the number of requests.

## Credits

This template was created with the help of [Claude Code](https://claude.com/claude-code) (Anthropic).

## License

MIT
