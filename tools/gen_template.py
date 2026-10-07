#!/usr/bin/env python3
"""Generator for the Zabbix 7.4 template "Proxmox Backup Server by HTTP"."""
import hashlib
import sys

import yaml

T = 'Proxmox Backup Server by HTTP'
API = '{$PBS.SCHEME}://{HOST.CONN}:{$PBS.PORT}/api2/json'
AUTH = [{'name': 'Authorization', 'value': 'PBSAPIToken={$PBS.TOKEN.ID}:{$PBS.TOKEN.SECRET}'}]
API_DOWN = {'name': 'PBS: API is unavailable', 'expression': f'last(/{T}/pbs.api.available)=0'}


def uid(*parts):
    h = hashlib.md5(('pbs-http-tpl|' + '|'.join(parts)).encode()).hexdigest()
    return h[:12] + '4' + h[13:16] + '89ab'[int(h[16], 16) % 4] + h[17:]


def tags(**kw):
    return [{'tag': k, 'value': v} for k, v in kw.items()]


def jsonpath(path, on_error=None, err_value=None):
    s = {'type': 'JSONPATH', 'parameters': [path]}
    if on_error:
        s['error_handler'] = on_error
        s['error_handler_params'] = ''
        if err_value is not None:
            s['error_handler_params'] = err_value
    return s


def js(code):
    return {'type': 'JAVASCRIPT', 'parameters': [code.strip('\n') + '\n']}


def mult(m):
    return {'type': 'MULTIPLIER', 'parameters': [str(m)]}


def heartbeat(t='1d'):
    return {'type': 'DISCARD_UNCHANGED_HEARTBEAT', 'parameters': [t]}


def trig(expr, name, prio, desc='', deps=(API_DOWN,), uid_key=None, **kw):
    t = {'uuid': uid('trigger', uid_key or name), 'expression': expr, 'name': name, 'priority': prio}
    t.update(kw)
    if desc:
        t['description'] = desc
    if deps:
        t['dependencies'] = [dict(d) for d in deps]
    t['tags'] = t.get('tags', [])
    if not t['tags']:
        del t['tags']
    return t


def http_item(name, key, path, delay, desc, query=None, prep=None, tag='raw', **kw):
    it = {
        'uuid': uid('item', key), 'name': name, 'type': 'HTTP_AGENT', 'key': key, 'delay': delay,
        'history': '0', 'value_type': 'TEXT', 'description': desc,
        'preprocessing': prep if prep is not None else [jsonpath('$.data')],
        'timeout': '{$PBS.TIMEOUT}', 'url': API + path, 'headers': AUTH,
        'tags': tags(component=tag),
    }
    if query:
        it['query_fields'] = [{'name': k, 'value': v} for k, v in query]
    it.update(kw)
    return it


def dep_item(name, key, master, prep, desc='', vt=None, units=None, tag=None, triggers=None,
             valuemap=None, history=None, extra_tags=None, **kw):
    it = {'uuid': uid('item', key), 'name': name, 'type': 'DEPENDENT', 'key': key, 'delay': '0'}
    if history:
        it['history'] = history
    if vt:
        it['value_type'] = vt
    if units:
        it['units'] = units
    if desc:
        it['description'] = desc
    if valuemap:
        it['valuemap'] = {'name': valuemap}
    it['preprocessing'] = prep
    it['master_item'] = {'key': master}
    tg = []
    if tag:
        tg.append({'tag': 'component', 'value': tag})
    tg += extra_tags or []
    if tg:
        it['tags'] = tg
    if triggers:
        it['triggers'] = triggers
    it.update(kw)
    return it


def lld_filter(conds):
    return {'evaltype': 'AND', 'conditions': [
        {'macro': m, 'value': v, 'operator': op, 'formulaid': chr(65 + i)}
        for i, (m, v, op) in enumerate(conds)]}


# --------------------------------------------------------------------------------------------
# Shared JavaScript snippets
# --------------------------------------------------------------------------------------------
JS_STATE_CODE = r'''
// 0 OK, 1 warnings, 2 error, 3 running, 4 never run
function stateCode(state, endtime, upid) {
    if (state === undefined || state === null || state === '') {
        return upid ? 3 : 4;
    }
    var s = String(state);
    if (s === 'OK' || s === 'ok') return 0;
    if (/^WARNINGS/i.test(s)) return 1;
    return 2;
}
'''

JS_TASKS = r'''
var period = parseFloat('{$PBS.TASKS.PERIOD}') * 3600;
var since = Math.floor(Date.now() / 1000) - period;
var tasks = JSON.parse(value);
var cats = ['all', 'backup', 'restore', 'sync', 'verify', 'prune', 'gc', 'tape', 'other'];
var out = {}, failed = [];
cats.forEach(function (c) { out[c] = {ok: 0, warning: 0, error: 0, running: 0}; });

function category(type) {
    if (/tape/.test(type)) return 'tape';
    if (type === 'backup') return 'backup';
    if (type === 'reader') return 'restore';
    if (/sync/.test(type)) return 'sync';
    if (/^verif/.test(type)) return 'verify';
    if (/prune/.test(type)) return 'prune';
    if (type === 'garbage_collection') return 'gc';
    return 'other';
}

function pad(n) { return n < 10 ? '0' + n : '' + n; }
function fmt(ts) {
    var d = new Date(ts * 1000);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' '
        + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

tasks.forEach(function (t) {
    var running = (t.endtime === undefined || t.endtime === null);
    if (!running && t.starttime < since) return;
    var st, s = String(t.status || '');
    if (running) st = 'running';
    else if (s === 'OK') st = 'ok';
    else if (/^WARNINGS/.test(s)) st = 'warning';
    else st = 'error';
    var c = category(String(t.worker_type || ''));
    out[c][st]++;
    out.all[st]++;
    if (st === 'error') failed.push(t);
});

failed.sort(function (a, b) { return b.starttime - a.starttime; });
out.failed = failed.length ? failed.slice(0, 30).map(function (t) {
    return fmt(t.starttime) + ' ' + t.worker_type + ' ' + (t.worker_id || '-') + ': ' + t.status;
}).join('\n') : 'none';
return JSON.stringify(out);
'''

# --------------------------------------------------------------------------------------------
items, discovery, top_triggers = [], [], []

# ---- API availability / version -------------------------------------------------------------
items.append(http_item(
    'PBS: API availability', 'pbs.api.available', '/version', '1m',
    'Checks that the PBS API answers to an authorized request: 1 = available, 0 = not available '
    '(connection error, TLS error, wrong token, HTTP error code).',
    prep=[{'type': 'CHECK_NOT_SUPPORTED', 'parameters': ['-1'], 'error_handler': 'CUSTOM_VALUE',
           'error_handler_params': '0'},
          js('return JSON.parse(value).data.version ? 1 : 0;')],
    tag='health', history='7d', value_type='UNSIGNED', valuemap={'name': 'PBS API state'},
    triggers=[trig(f'last(/{T}/pbs.api.available)=0', 'PBS: API is unavailable', 'HIGH',
                   'PBS API does not answer or rejects the token. Check network, the proxmox-backup-proxy '
                   'service, {$PBS.TOKEN.ID} / {$PBS.TOKEN.SECRET} macros and the token permissions.',
                   deps=())]))

items.append(http_item(
    'PBS: Version', 'pbs.version', '/version', '1h', 'Proxmox Backup Server version.',
    prep=[js('var d = JSON.parse(value).data;\nreturn d.version + (d.release !== undefined ? "." + d.release : "");'),
          heartbeat()],
    tag='system', history='30d', value_type='CHAR',
    triggers=[trig(f'last(/{T}/pbs.version,#1)<>last(/{T}/pbs.version,#2) and length(last(/{T}/pbs.version))>0',
                   'PBS: Version has changed', 'INFO', 'PBS version has changed. Acknowledge to close.',
                   manual_close='YES')]))

# ---- Node status -----------------------------------------------------------------------------
NS = 'pbs.node.status'
items.append(http_item('PBS: Get node status', NS, '/nodes/{$PBS.NODE}/status', '1m',
                       'Raw node status: CPU, memory, swap, root filesystem, load average, uptime.'))

items += [
    dep_item('PBS: CPU utilization', 'pbs.cpu.util', NS, [jsonpath('$.cpu'), mult(100)],
             'CPU utilization in %.', 'FLOAT', '%', 'cpu', triggers=[trig(
                 f'min(/{T}/pbs.cpu.util,5m)>{{$PBS.CPU.UTIL.CRIT}}',
                 'PBS: High CPU utilization (over {$PBS.CPU.UTIL.CRIT}% for 5m)', 'WARNING',
                 'CPU utilization is too high. Backup, verify and GC jobs may run slower.',
                 opdata='Current utilization: {ITEM.LASTVALUE1}')]),
    dep_item('PBS: CPU iowait', 'pbs.cpu.iowait', NS,
             [jsonpath('$.wait', 'DISCARD_VALUE'), mult(100)],
             'Share of CPU time spent waiting for I/O, in %. High values usually mean slow datastore disks.',
             'FLOAT', '%', 'cpu', triggers=[trig(
                 f'min(/{T}/pbs.cpu.iowait,10m)>{{$PBS.CPU.IOWAIT.MAX}}',
                 'PBS: High CPU iowait (over {$PBS.CPU.IOWAIT.MAX}% for 10m)', 'WARNING',
                 'Disks cannot keep up with the I/O load.',
                 opdata='Current iowait: {ITEM.LASTVALUE1}')]),
    dep_item('PBS: Number of CPUs', 'pbs.cpu.num', NS, [jsonpath('$.cpuinfo.cpus'), heartbeat()],
             'Number of logical CPUs.', tag='cpu'),
    dep_item('PBS: CPU model', 'pbs.cpu.model', NS, [jsonpath('$.cpuinfo.model'), heartbeat()],
             'CPU model.', 'CHAR', tag='cpu'),
]
for i, m in enumerate(['1', '5', '15']):
    items.append(dep_item(f'PBS: Load average ({m}m avg)', f'pbs.load[avg{m}]', NS,
                          [jsonpath(f'$.loadavg[{i}]')], f'System load average over {m} minute(s).',
                          'FLOAT', tag='cpu'))
top_triggers.append(trig(
    f'min(/{T}/pbs.load[avg1],5m)/last(/{T}/pbs.cpu.num)>{{$PBS.LOAD.PER.CPU.MAX}} '
    f'and last(/{T}/pbs.load[avg5])>0 and last(/{T}/pbs.load[avg15])>=0',
    'PBS: Load average is too high (per CPU load over {$PBS.LOAD.PER.CPU.MAX} for 5m)', 'AVERAGE',
    'Per-CPU load average is too high.', opdata='Load averages(1m, 5m, 15m): ({ITEM.LASTVALUE1}, '
    '{ITEM.LASTVALUE3}, {ITEM.LASTVALUE4}), # of CPUs: {ITEM.LASTVALUE2}'))

items += [
    dep_item('PBS: Memory total', 'pbs.memory.total', NS, [jsonpath('$.memory.total')], 'Total memory.',
             units='B', tag='memory'),
    dep_item('PBS: Memory used', 'pbs.memory.used', NS, [jsonpath('$.memory.used')], 'Used memory.',
             units='B', tag='memory'),
    dep_item('PBS: Memory utilization', 'pbs.memory.pused', NS,
             [js('var m = JSON.parse(value).memory;\nreturn m.total > 0 ? m.used / m.total * 100 : 0;')],
             'Memory utilization in %.', 'FLOAT', '%', 'memory', triggers=[trig(
                 f'min(/{T}/pbs.memory.pused,5m)>{{$PBS.MEMORY.PUSED.MAX}}',
                 'PBS: High memory utilization (over {$PBS.MEMORY.PUSED.MAX}% for 5m)', 'AVERAGE',
                 'The system is running out of free memory.', opdata='Used: {ITEM.LASTVALUE1}')]),
    dep_item('PBS: Swap total', 'pbs.swap.total', NS, [jsonpath('$.swap.total', 'DISCARD_VALUE')],
             'Total swap.', units='B', tag='memory'),
    dep_item('PBS: Swap used', 'pbs.swap.used', NS, [jsonpath('$.swap.used', 'DISCARD_VALUE')],
             'Used swap.', units='B', tag='memory'),
    dep_item('PBS: Root filesystem total', 'pbs.rootfs.total', NS, [jsonpath('$.root.total')],
             'Root filesystem size.', units='B', tag='storage'),
    dep_item('PBS: Root filesystem used', 'pbs.rootfs.used', NS, [jsonpath('$.root.used')],
             'Root filesystem used space.', units='B', tag='storage'),
    dep_item('PBS: Root filesystem available', 'pbs.rootfs.avail', NS, [jsonpath('$.root.avail')],
             'Root filesystem available space.', units='B', tag='storage'),
    dep_item('PBS: Root filesystem utilization', 'pbs.rootfs.pused', NS,
             [js('var r = JSON.parse(value).root;\nreturn r.total > 0 ? r.used / r.total * 100 : 0;')],
             'Root filesystem utilization in %.', 'FLOAT', '%', 'storage', triggers=[trig(
                 f'last(/{T}/pbs.rootfs.pused)>{{$PBS.ROOTFS.PUSED.MAX}}',
                 'PBS: Root filesystem is low on space (over {$PBS.ROOTFS.PUSED.MAX}% used)', 'AVERAGE',
                 'Root filesystem of PBS is almost full. Logs and task archive may stop being written.',
                 opdata='Used: {ITEM.LASTVALUE1}')]),
    dep_item('PBS: Uptime', 'pbs.uptime', NS, [jsonpath('$.uptime')], 'System uptime.', units='uptime',
             tag='system', triggers=[trig(
                 f'last(/{T}/pbs.uptime)<10m', 'PBS: Host has been restarted', 'INFO',
                 'Uptime is less than 10 minutes.', opdata='Uptime: {ITEM.LASTVALUE1}', manual_close='YES')]),
    dep_item('PBS: Kernel version', 'pbs.kernel', NS, [jsonpath('$.kversion'), heartbeat()],
             'Running kernel version.', 'CHAR', tag='system'),
]

# ---- Tasks -----------------------------------------------------------------------------------
TK = 'pbs.tasks'
items.append(http_item(
    'PBS: Get tasks', TK, '/nodes/{$PBS.NODE}/tasks', '{$PBS.TASKS.INTERVAL}',
    'Task list of the node, aggregated over the last {$PBS.TASKS.PERIOD} hours: number of OK / warning / '
    'error / running tasks per category and the list of failed tasks.\n'
    'Categories: backup (client backups pushed from PVE / proxmox-backup-client), restore, sync, verify, '
    'prune, gc, tape, other.',
    query=[('limit', '{$PBS.TASKS.LIMIT}')],
    prep=[jsonpath('$.data'), js(JS_TASKS)]))

CAT_NAMES = {'all': 'All tasks', 'backup': 'Backup tasks', 'restore': 'Restore tasks', 'sync': 'Sync tasks',
             'verify': 'Verify tasks', 'prune': 'Prune tasks', 'gc': 'GC tasks', 'tape': 'Tape tasks',
             'other': 'Other tasks'}
ST_NAMES = {'ok': 'OK', 'warning': 'with warnings', 'error': 'failed', 'running': 'running'}
for c, cn in CAT_NAMES.items():
    for st, sn in ST_NAMES.items():
        if st == 'running' and c != 'all':
            continue
        key = f'pbs.tasks.count[{c},{st}]'
        trg = None
        if st == 'error' and c in ('backup', 'other', 'restore'):
            prio = {'backup': 'HIGH', 'restore': 'AVERAGE', 'other': 'WARNING'}[c]
            trg = [trig(f'last(/{T}/{key})>0',
                        f'PBS: {cn} failed in the last {{$PBS.TASKS.PERIOD}}h', prio,
                        f'There are failed tasks of category "{c}" during the last {{$PBS.TASKS.PERIOD}} hours. '
                        'See item "PBS: Failed tasks list" for details. The problem resolves itself when the '
                        'failed tasks age out of the period, or can be closed manually.',
                        opdata='Failed: {ITEM.LASTVALUE1}', manual_close='YES')]
        items.append(dep_item(
            f'PBS: {cn} {sn} (period)' if st != 'running' else 'PBS: Running tasks',
            key, TK, [jsonpath(f'$.{c}.{st}')],
            f'Number of {cn.lower()} {sn} during the last {{$PBS.TASKS.PERIOD}} hours.'
            if st != 'running' else 'Number of currently running tasks.',
            tag='tasks', triggers=trg, extra_tags=[{'tag': 'task', 'value': c}]))
items.append(dep_item('PBS: Failed tasks list', 'pbs.tasks.failed', TK, [jsonpath('$.failed'), heartbeat('1h')],
                      'Up to 30 most recent failed tasks during the last {$PBS.TASKS.PERIOD} hours '
                      '(time, type, id, error).', 'TEXT', tag='tasks', history='7d'))

# ---- Other node level checks -----------------------------------------------------------------
items.append(http_item(
    'PBS: Subscription status', 'pbs.subscription.status', '/nodes/{$PBS.NODE}/subscription', '6h',
    'Subscription status: new, notfound, active, invalid, expired, suspended.',
    prep=[jsonpath('$.data.status'), heartbeat()], tag='system', history='30d', value_type='CHAR',
    triggers=[trig(f'find(/{T}/pbs.subscription.status,,"regexp","^(invalid|expired|suspended)$")=1',
                   'PBS: Subscription is not valid', 'INFO',
                   'Subscription is invalid, expired or suspended. Not a problem for no-subscription setups '
                   '(status "notfound").', opdata='Status: {ITEM.LASTVALUE1}')]))

items.append(http_item(
    'PBS: Available package updates', 'pbs.apt.updates', '/nodes/{$PBS.NODE}/apt/update', '6h',
    'Number of packages with available updates (from the last apt update run by PBS).',
    prep=[js('return JSON.parse(value).data.length;')], tag='system', history='30d', value_type='UNSIGNED',
    triggers=[trig(f'last(/{T}/pbs.apt.updates)>0', 'PBS: Package updates are available', 'INFO',
                   'There are package updates available on PBS.', opdata='Updates: {ITEM.LASTVALUE1}',
                   manual_close='YES', status='DISABLED')]))

items.append(http_item(
    'PBS: API certificate expires', 'pbs.cert.expiry', '/nodes/{$PBS.NODE}/certificates/info', '6h',
    'Expiry date of the certificate used by the PBS API/web UI (proxy.pem).',
    prep=[js(r'''
var certs = JSON.parse(value).data, min = 0;
certs.forEach(function (c) {
    if (c.filename === 'proxy.pem' && c.notafter) min = c.notafter;
});
if (!min) {
    certs.forEach(function (c) { if (c.notafter && (!min || c.notafter < min)) min = c.notafter; });
}
if (!min) throw 'No certificates found';
return min;
''')], tag='system', history='30d', value_type='UNSIGNED', units='unixtime',
    triggers=[trig(f'(last(/{T}/pbs.cert.expiry)-now())/86400<{{$PBS.CERT.EXPIRY.WARN}}',
                   'PBS: API certificate expires in less than {$PBS.CERT.EXPIRY.WARN} days', 'WARNING',
                   'Renew the PBS certificate (ACME or manual).', opdata='Expires: {ITEM.LASTVALUE1}')]))

# ---- Datastores (with nested namespace / backup group discovery) -----------------------------
DS = 'pbs.datastore.usage'
items.append(http_item('PBS: Get datastore usage', DS, '/status/datastore-usage', '5m',
                       'Raw datastore usage of all datastores.'))


def ds_js(expr):
    return js("var s = JSON.parse(value).filter(function (d) { return d.store === '{#STORE}'; })[0];\n"
              "if (!s) throw 'Datastore {#STORE} not found';\n" + expr)


DST = [{'tag': 'datastore', 'value': '{#STORE}'}]
ds_protos = [
    dep_item('PBS: Datastore [{#STORE}]: Total space', 'pbs.datastore.total[{#STORE}]', DS,
             [jsonpath("$[?(@.store == '{#STORE}')].total.first()", 'DISCARD_VALUE')],
             'Total space of the datastore.', units='B', tag='datastore', extra_tags=DST),
    dep_item('PBS: Datastore [{#STORE}]: Used space', 'pbs.datastore.used[{#STORE}]', DS,
             [jsonpath("$[?(@.store == '{#STORE}')].used.first()", 'DISCARD_VALUE')],
             'Used space of the datastore.', units='B', tag='datastore', extra_tags=DST),
    dep_item('PBS: Datastore [{#STORE}]: Available space', 'pbs.datastore.avail[{#STORE}]', DS,
             [jsonpath("$[?(@.store == '{#STORE}')].avail.first()", 'DISCARD_VALUE')],
             'Available space of the datastore.', units='B', tag='datastore', extra_tags=DST),
    dep_item('PBS: Datastore [{#STORE}]: Space utilization', 'pbs.datastore.pused[{#STORE}]', DS,
             [jsonpath("$[?(@.store == '{#STORE}' && @.total > 0)].first()", 'DISCARD_VALUE'),
              js('var s = JSON.parse(value);\nreturn s.used / s.total * 100;')],
             'Used space of the datastore in %.', 'FLOAT', '%', 'datastore', extra_tags=DST),
    dep_item('PBS: Datastore [{#STORE}]: Estimated full date', 'pbs.datastore.full_date[{#STORE}]', DS,
             [ds_js("var e = s['estimated-full-date'];\nreturn (e && e > 0) ? e : 0;")],
             'Date when the datastore is estimated to be full (PBS linear forecast). 0 = not filling up / '
             'not enough history.', units='unixtime', tag='datastore', extra_tags=DST),
    dep_item('PBS: Datastore [{#STORE}]: Error', 'pbs.datastore.error[{#STORE}]', DS,
             [ds_js("return s.error ? String(s.error) : 'none';"), heartbeat('1h')],
             'Error reported by PBS for the datastore (unavailable mount, I/O error, etc). "none" = no error.',
             'CHAR', tag='datastore', extra_tags=DST, history='7d'),
]
ds_trigs = [
    trig(f'last(/{T}/pbs.datastore.pused[{{#STORE}}])>{{$PBS.DATASTORE.PUSED.CRIT:"{{#STORE}}"}}',
         'PBS: Datastore [{#STORE}]: Space is critically low (used > {$PBS.DATASTORE.PUSED.CRIT:"{#STORE}"}%)',
         'HIGH', 'The datastore is almost full. New backups will fail when it is full. Run prune + GC or '
         'add space.', opdata='Used: {ITEM.LASTVALUE1}', uid_key='ds-crit'),
    trig(f'last(/{T}/pbs.datastore.pused[{{#STORE}}])>{{$PBS.DATASTORE.PUSED.WARN:"{{#STORE}}"}}',
         'PBS: Datastore [{#STORE}]: Space is low (used > {$PBS.DATASTORE.PUSED.WARN:"{#STORE}"}%)',
         'WARNING', 'The datastore is filling up.', opdata='Used: {ITEM.LASTVALUE1}', uid_key='ds-warn',
         deps=(API_DOWN, {
             'name': 'PBS: Datastore [{#STORE}]: Space is critically low (used > {$PBS.DATASTORE.PUSED.CRIT:"{#STORE}"}%)',
             'expression': f'last(/{T}/pbs.datastore.pused[{{#STORE}}])>{{$PBS.DATASTORE.PUSED.CRIT:"{{#STORE}}"}}'})),
    trig(f'last(/{T}/pbs.datastore.full_date[{{#STORE}}])>0 and '
         f'(last(/{T}/pbs.datastore.full_date[{{#STORE}}])-now())/86400<{{$PBS.DATASTORE.FULL.DAYS:"{{#STORE}}"}}',
         'PBS: Datastore [{#STORE}]: Estimated to be full in less than {$PBS.DATASTORE.FULL.DAYS:"{#STORE}"} days',
         'WARNING', 'Based on usage history PBS estimates that the datastore will be full soon.',
         opdata='Full at: {ITEM.LASTVALUE1}', uid_key='ds-full'),
    trig(f'last(/{T}/pbs.datastore.error[{{#STORE}}])<>"none"',
         'PBS: Datastore [{#STORE}]: Datastore reports an error', 'HIGH',
         'PBS reports an error for the datastore (e.g. not mounted, I/O error).',
         opdata='{ITEM.LASTVALUE1}', uid_key='ds-error'),
]
ds_protos[3]['trigger_prototypes'] = ds_trigs[:2]
ds_protos[4]['trigger_prototypes'] = [ds_trigs[2]]
ds_protos[5]['trigger_prototypes'] = [ds_trigs[3]]

discovery.append({
    'uuid': uid('lld', 'pbs.datastore.discovery'), 'name': 'PBS: Datastore discovery', 'type': 'DEPENDENT',
    'key': 'pbs.datastore.discovery', 'delay': '0',
    'filter': lld_filter([('{#STORE}', '{$PBS.DATASTORE.MATCHES}', 'MATCHES_REGEX'),
                          ('{#STORE}', '{$PBS.DATASTORE.NOT_MATCHES}', 'NOT_MATCHES_REGEX')]),
    'lifetime': '{$PBS.LLD.LIFETIME}', 'enabled_lifetime_type': 'DISABLE_NEVER',
    'description': 'Discovers datastores.',
    'item_prototypes': ds_protos,
    'master_item': {'key': DS},
    'lld_macro_paths': [{'lld_macro': '{#STORE}', 'path': '$.store'}],
})

# Level 2: namespaces of a datastore (HTTP per datastore)
discovery.append({
    'uuid': uid('lld', 'pbs.namespace.discovery'), 'name': 'PBS: Datastore [{#STORE}]: Namespace discovery',
    'type': 'HTTP_AGENT', 'key': 'pbs.namespace.discovery[{#STORE}]', 'delay': '1h',
    'lifetime': '{$PBS.LLD.LIFETIME}', 'enabled_lifetime_type': 'DISABLE_NEVER',
    'description': 'Discovers namespaces of the datastore. The root namespace is always included.',
    'parent_discovery_rule': {'key': 'pbs.datastore.discovery'},
    'timeout': '{$PBS.TIMEOUT}', 'url': API + '/admin/datastore/{#STORE}/namespace', 'headers': AUTH,
    'lld_macro_paths': [{'lld_macro': '{#NS}', 'path': '$.ns'},
                        {'lld_macro': '{#NS.LABEL}', 'path': '$.label'},
                        {'lld_macro': '{#NS.QUERY}', 'path': '$.query'}],
    'preprocessing': [js(r'''
var data = JSON.parse(value).data || [], seen = {}, out = [];
data.unshift({ns: ''});
data.forEach(function (n) {
    var ns = n.ns || '';
    if (seen[ns]) return;
    seen[ns] = true;
    out.push({ns: ns, label: ns === '' ? '/' : '/' + ns + '/', query: ns === '' ? '' : '?ns=' + encodeURIComponent(ns)});
});
return JSON.stringify(out);
''')],
})

# Level 3: backup groups of a namespace. One raw HTTP item per group (cross-level dependent items are
# not allowed by Zabbix), everything else depends on it.
GKEY = '["{#STORE}","{#NS}","{#BTYPE}","{#BID}"]'
GNAME = 'PBS: Backup [{#STORE}{#NS.LABEL}{#BTYPE}/{#BID}]'
GT = [{'tag': 'datastore', 'value': '{#STORE}'}, {'tag': 'backup_group', 'value': '{#BTYPE}/{#BID}'}]
GRAW = 'pbs.group.raw' + GKEY
GAGE = 'pbs.group.age' + GKEY
G_CRIT = f'last(/{T}/{GAGE})>{{$PBS.GROUP.AGE.CRIT:"{{#BTYPE}}/{{#BID}}"}}*3600'
G_CRIT_NAME = GNAME + ': No backup for more than {$PBS.GROUP.AGE.CRIT:"{#BTYPE}/{#BID}"}h'
grp_raw = http_item(
    GNAME + ': Get group data', GRAW, '/admin/datastore/{#STORE}/groups{#NS.QUERY}', '{$PBS.GROUP.INTERVAL}',
    'Raw data of the backup group (taken from the group list of its namespace).',
    prep=[js("var g = JSON.parse(value).data.filter(function (x) {\n"
             "    return x['backup-type'] === '{#BTYPE}' && x['backup-id'] === '{#BID}';\n"
             "})[0];\n"
             "if (!g) throw 'Backup group {#BTYPE}/{#BID} not found';\n"
             "return JSON.stringify(g);")])
grp_raw['tags'] += GT
grp_raw['uuid'] = uid('item', 'pbs.group.raw')
grp_items = [
    grp_raw,
    dep_item(GNAME + ': Last backup age', GAGE, GRAW,
             [js("return Math.max(0, Math.floor(Date.now() / 1000) - JSON.parse(value)['last-backup']);")],
             'Time since the newest snapshot of the group. On a sync target the snapshot time is the original '
             'backup time, so this shows end-to-end freshness.', units='s', tag='backup', extra_tags=GT,
             history='30d', trigger_prototypes=[
                 trig(G_CRIT, G_CRIT_NAME, 'HIGH',
                      'There is no new snapshot of this backup group for a long time. Check the backup job on the '
                      'PVE side / client. If the guest was deleted: prune the group, exclude it with '
                      '{$PBS.GROUP.NOT_MATCHES} or put "nomon" into the group comment in PBS.',
                      opdata='Last backup: {ITEM.LASTVALUE1} ago', uid_key='grp-crit', manual_close='YES'),
                 trig(f'last(/{T}/{GAGE})>{{$PBS.GROUP.AGE.WARN:"{{#BTYPE}}/{{#BID}}"}}*3600',
                      GNAME + ': No backup for more than {$PBS.GROUP.AGE.WARN:"{#BTYPE}/{#BID}"}h', 'WARNING',
                      'The last backup run of this group was probably missed or failed.',
                      opdata='Last backup: {ITEM.LASTVALUE1} ago', uid_key='grp-warn', manual_close='YES',
                      deps=(API_DOWN, {'name': G_CRIT_NAME, 'expression': G_CRIT})),
             ]),
    dep_item(GNAME + ': Last backup time', 'pbs.group.last' + GKEY, GRAW,
             [jsonpath("$['last-backup']"), heartbeat()], 'Time of the newest snapshot.', units='unixtime',
             tag='backup', extra_tags=GT, history='30d'),
    dep_item(GNAME + ': Snapshot count', 'pbs.group.count' + GKEY, GRAW,
             [jsonpath("$['backup-count']")], 'Number of snapshots in the group.', tag='backup',
             extra_tags=GT, history='30d'),
]
for it in grp_items:
    it['uuid'] = uid('item', it['key'].split('[')[0])
discovery.append({
    'uuid': uid('lld', 'pbs.group.discovery'),
    'name': 'PBS: Datastore [{#STORE}{#NS.LABEL}]: Backup group discovery',
    'type': 'HTTP_AGENT', 'key': 'pbs.group.discovery["{#STORE}","{#NS}"]', 'delay': '1h',
    'lifetime': '{$PBS.LLD.LIFETIME}', 'enabled_lifetime_type': 'DISABLE_NEVER',
    'description': 'Discovers backup groups (vm/<id>, ct/<id>, host/<name>) of the namespace. Filter with '
                   '{$PBS.GROUP.MATCHES} / {$PBS.GROUP.NOT_MATCHES} (regex on "<store><namespace><type>/<id>", '
                   'e.g. "store1/vm/100"). Groups whose comment in PBS matches {$PBS.GROUP.IGNORE.COMMENT} '
                   'get no freshness triggers (useful for deleted guests whose backups are kept).',
    'parent_discovery_rule': {'key': 'pbs.namespace.discovery[{#STORE}]'},
    'filter': lld_filter([('{#GROUP}', '{$PBS.GROUP.MATCHES}', 'MATCHES_REGEX'),
                          ('{#GROUP}', '{$PBS.GROUP.NOT_MATCHES}', 'NOT_MATCHES_REGEX')]),
    'timeout': '{$PBS.TIMEOUT}', 'url': API + '/admin/datastore/{#STORE}/groups{#NS.QUERY}', 'headers': AUTH,
    'lld_macro_paths': [{'lld_macro': '{#BTYPE}', 'path': "$['backup-type']"},
                        {'lld_macro': '{#BID}', 'path': "$['backup-id']"},
                        {'lld_macro': '{#GROUP}', 'path': '$.group'},
                        {'lld_macro': '{#BCOMMENT}', 'path': '$.comment'}],
    'preprocessing': [js(r"""
var data = JSON.parse(value).data || [];
return JSON.stringify(data.map(function (g) {
    return {
        'backup-type': g['backup-type'],
        'backup-id': g['backup-id'],
        group: '{#STORE}{#NS.LABEL}' + g['backup-type'] + '/' + g['backup-id'],
        comment: g.comment || ''
    };
}));
""")],
    'item_prototypes': grp_items,
    'overrides': [{
        'name': 'No freshness triggers for groups marked in comment', 'step': '1',
        'filter': {'evaltype': 'AND', 'conditions': [
            {'macro': '{#BCOMMENT}', 'value': '{$PBS.GROUP.IGNORE.COMMENT}', 'operator': 'MATCHES_REGEX',
             'formulaid': 'A'}]},
        'operations': [{'operationobject': 'TRIGGER_PROTOTYPE', 'operator': 'LIKE', 'value': 'No backup for',
                        'discover': 'NO_DISCOVER'}],
    }],
})

# ---- Scheduled jobs (sync / verify / prune / gc / tape) --------------------------------------
JOBS = [
    ('sync', 'Sync job', '/admin/sync', 'AVERAGE'),
    ('verify', 'Verify job', '/admin/verify', 'HIGH'),
    ('prune', 'Prune job', '/admin/prune', 'AVERAGE'),
    ('gc', 'Garbage collection', '/admin/gc', 'AVERAGE'),
    ('tape', 'Tape backup job', '/tape/backup', 'AVERAGE'),
]
JS_JOBS = JS_STATE_CODE + r'''
var now = Math.floor(Date.now() / 1000);
return JSON.stringify((JSON.parse(value).data || []).map(function (j) {
    var state = j['last-run-state'], upid = j['last-run-upid'] || j.upid;
    return {
        id: String(j.id || j.store),
        store: j.store || '',
        remote: j.remote ? j.remote + ':' + (j['remote-store'] || '') : '',
        schedule: j.schedule || '',
        comment: j.comment || '',
        state: state ? String(state) : (upid ? 'running' : 'never run'),
        code: stateCode(state, j['last-run-endtime'], upid),
        last: j['last-run-endtime'] || 0,
        next: j['next-run'] || 0,
        duration: j.duration || 0,
        pending: j['pending-bytes'],
        removed: j['removed-bytes']
    };
}));
'''

for jt, jn, path, prio in JOBS:
    raw_key = f'pbs.jobs[{jt}]'
    items.append(http_item(f'PBS: Get {jn.lower()} status', raw_key, path, '{$PBS.JOBS.INTERVAL}',
                           f'Raw status of all {jn.lower()}s ({path}).', prep=[js(JS_JOBS)]))
    sel = (f"var j = JSON.parse(value).filter(function (x) {{ return x.id === '{{#JOB.ID}}'; }})[0];\n"
           f"if (!j) throw 'Job {{#JOB.ID}} not found';\n")
    jtags = [{'tag': 'job_type', 'value': jt}, {'tag': 'job', 'value': '{#JOB.ID}'}]
    JNAME = f'PBS: {jn} [{{#JOB.ID}}]'
    k = lambda n: f'pbs.job.{n}[{jt},"{{#JOB.ID}}"]'
    state_trig_failed = trig(
        f'last(/{T}/{k("state")})=2', JNAME + ': Last run failed', prio,
        f'The last run of the {jn.lower()} ended with an error. Error text is in item '
        f'"{jn} [{{#JOB.ID}}]: Last run result".', opdata='{ITEM.LASTVALUE1}', uid_key=f'job-{jt}-fail')
    protos = [
        dep_item(JNAME + ': Last run state', k('state'), raw_key, [js(sel + 'return j.code;')],
                 'Result of the last run: 0 OK, 1 warnings, 2 error, 3 running, 4 never run.',
                 tag='jobs', valuemap='PBS job state', extra_tags=jtags, history='30d'),
        dep_item(JNAME + ': Last run result', k('result'), raw_key, [js(sel + 'return j.state;'), heartbeat('1d')],
                 'Last run result text ("OK", "WARNINGS: n" or the error message).', 'CHAR', tag='jobs',
                 extra_tags=jtags, history='30d'),
        dep_item(JNAME + ': Last run end', k('last'), raw_key, [js(sel + 'return j.last;')],
                 'End time of the last run (0 = never).', units='unixtime', tag='jobs', extra_tags=jtags),
        dep_item(JNAME + ': Next run', k('next'), raw_key, [js(sel + 'return j.next;')],
                 'Next scheduled run (0 = not scheduled).', units='unixtime', tag='jobs', extra_tags=jtags),
    ]
    protos[0]['trigger_prototypes'] = [
        state_trig_failed,
        trig(f'last(/{T}/{k("state")})=1', JNAME + ': Last run finished with warnings', 'WARNING',
             f'The last run of the {jn.lower()} finished with warnings.', uid_key=f'job-{jt}-warn',
             deps=(API_DOWN, {'name': state_trig_failed['name'], 'expression': state_trig_failed['expression']})),
    ]
    protos[2]['trigger_prototypes'] = [trig(
        f'last(/{T}/{k("next")})>0 and last(/{T}/{k("last")})>0 and '
        f'(now()-last(/{T}/{k("last")}))/3600>{{$PBS.JOB.MAX.AGE:"{jt}"}}',
        JNAME + f': Has not run for more than {{$PBS.JOB.MAX.AGE:"{jt}"}}h', 'WARNING',
        'The job is scheduled, but its last run is too old. Check that the schedule is correct and '
        'that the job is not stuck.', opdata='Last run: {ITEM.LASTVALUE2}', uid_key=f'job-{jt}-age')]
    if jt == 'gc':
        protos.append(dep_item(JNAME + ': Pending bytes', k('pending'), raw_key,
                               [js(sel + "if (j.pending === undefined || j.pending === null) throw 'no data';\n"
                                         "return j.pending;")],
                               'Bytes that can be removed by the next garbage collection.', units='B',
                               tag='jobs', extra_tags=jtags))
    macros = [('{#JOB.ID}', '$.id'), ('{#JOB.STORE}', '$.store'), ('{#JOB.SCHEDULE}', '$.schedule'),
              ('{#JOB.COMMENT}', '$.comment')]
    discovery.append({
        'uuid': uid('lld', f'pbs.jobs.discovery[{jt}]'), 'name': f'PBS: {jn} discovery', 'type': 'DEPENDENT',
        'key': f'pbs.jobs.discovery[{jt}]', 'delay': '0',
        'filter': lld_filter([('{#JOB.ID}', '{$PBS.JOB.MATCHES}', 'MATCHES_REGEX'),
                              ('{#JOB.ID}', '{$PBS.JOB.NOT_MATCHES}', 'NOT_MATCHES_REGEX')]),
        'lifetime': '{$PBS.LLD.LIFETIME}', 'enabled_lifetime_type': 'DISABLE_NEVER',
        'description': f'Discovers configured {jn.lower()}s.',
        'item_prototypes': protos,
        'master_item': {'key': raw_key},
        'lld_macro_paths': [{'lld_macro': m, 'path': p} for m, p in macros],
    })

# ---- Disks (SMART) ---------------------------------------------------------------------------
DK = 'pbs.disks'
items.append(http_item('PBS: Get disks', DK, '/nodes/{$PBS.NODE}/disks/list', '1h',
                       'Raw list of physical disks with SMART status (PBS runs smartctl for this request).'))
dsel = ("var d = JSON.parse(value).filter(function (x) { return x.name === '{#DISK}'; })[0];\n"
        "if (!d) throw 'Disk {#DISK} not found';\n")
DT = [{'tag': 'disk', 'value': '{#DISK}'}]
discovery.append({
    'uuid': uid('lld', 'pbs.disk.discovery'), 'name': 'PBS: Disk discovery', 'type': 'DEPENDENT',
    'key': 'pbs.disk.discovery', 'delay': '0',
    'filter': lld_filter([('{#DISK}', '{$PBS.DISK.MATCHES}', 'MATCHES_REGEX'),
                          ('{#DISK}', '{$PBS.DISK.NOT_MATCHES}', 'NOT_MATCHES_REGEX')]),
    'lifetime': '{$PBS.LLD.LIFETIME}', 'enabled_lifetime_type': 'DISABLE_NEVER',
    'description': 'Discovers physical disks.',
    'item_prototypes': [
        dep_item('PBS: Disk [{#DISK}]: SMART status', 'pbs.disk.smart[{#DISK}]', DK,
                 [js(dsel + "return d.status === 'passed' ? 0 : (d.status === 'failed' ? 1 : 2);")],
                 'SMART overall health: 0 passed, 1 failed, 2 unknown.', tag='disk', valuemap='PBS SMART status',
                 extra_tags=DT, history='30d', trigger_prototypes=[trig(
                     f'last(/{T}/pbs.disk.smart[{{#DISK}}])=1',
                     'PBS: Disk [{#DISK}]: SMART health check failed', 'HIGH',
                     'SMART reports that the disk is failing. Replace it.', uid_key='disk-smart')]),
        dep_item('PBS: Disk [{#DISK}]: Wearout', 'pbs.disk.wearout[{#DISK}]', DK,
                 [jsonpath("$[?(@.name == '{#DISK}')].wearout.first()", 'DISCARD_VALUE')],
                 'SSD wearout as shown in the PBS UI (percentage of used life). Absent for HDD.', 'FLOAT', '%',
                 'disk', extra_tags=DT, history='30d', trigger_prototypes=[trig(
                     f'last(/{T}/pbs.disk.wearout[{{#DISK}}])>{{$PBS.DISK.WEAROUT.MAX}}',
                     'PBS: Disk [{#DISK}]: SSD wearout is high (over {$PBS.DISK.WEAROUT.MAX}%)', 'WARNING',
                     'The SSD has used most of its rated write endurance. Plan the replacement.',
                     opdata='Wearout: {ITEM.LASTVALUE1}', uid_key='disk-wear')]),
        dep_item('PBS: Disk [{#DISK}]: Size', 'pbs.disk.size[{#DISK}]', DK, [js(dsel + 'return d.size;'),
                 heartbeat()], 'Disk size.', units='B', tag='disk', extra_tags=DT),
        dep_item('PBS: Disk [{#DISK}]: Usage', 'pbs.disk.usage[{#DISK}]', DK,
                 [js(dsel + "return d.used || 'unused';"), heartbeat()],
                 'What the disk is used for (zfs, lvm, mounted, partitions, unused...).', 'CHAR', tag='disk',
                 extra_tags=DT),
    ],
    'master_item': {'key': DK},
    'lld_macro_paths': [{'lld_macro': '{#DISK}', 'path': '$.name'},
                        {'lld_macro': '{#DISK.MODEL}', 'path': '$.model'},
                        {'lld_macro': '{#DISK.SERIAL}', 'path': '$.serial'}],
})

# ---- ZFS pools -------------------------------------------------------------------------------
ZF = 'pbs.zfs'
items.append(http_item('PBS: Get ZFS pools', ZF, '/nodes/{$PBS.NODE}/disks/zfs', '5m',
                       'Raw list of ZFS pools (empty if ZFS is not used).'))
zsel = ("var p = JSON.parse(value).filter(function (x) { return x.name === '{#POOL}'; })[0];\n"
        "if (!p) throw 'Pool {#POOL} not found';\n")
ZT = [{'tag': 'zpool', 'value': '{#POOL}'}]
discovery.append({
    'uuid': uid('lld', 'pbs.zfs.discovery'), 'name': 'PBS: ZFS pool discovery', 'type': 'DEPENDENT',
    'key': 'pbs.zfs.discovery', 'delay': '0',
    'lifetime': '{$PBS.LLD.LIFETIME}', 'enabled_lifetime_type': 'DISABLE_NEVER',
    'description': 'Discovers ZFS pools.',
    'item_prototypes': [
        dep_item('PBS: ZFS pool [{#POOL}]: Health', 'pbs.zfs.health[{#POOL}]', ZF,
                 [js(zsel + 'return p.health;'), heartbeat('1h')], 'Pool health (ONLINE, DEGRADED, FAULTED...).',
                 'CHAR', tag='zfs', extra_tags=ZT, history='30d', trigger_prototypes=[trig(
                     f'last(/{T}/pbs.zfs.health[{{#POOL}}])<>"ONLINE"',
                     'PBS: ZFS pool [{#POOL}] is not healthy', 'HIGH',
                     'ZFS pool state is not ONLINE. Check "zpool status".', opdata='State: {ITEM.LASTVALUE1}',
                     uid_key='zfs-health')]),
        dep_item('PBS: ZFS pool [{#POOL}]: Size', 'pbs.zfs.size[{#POOL}]', ZF, [js(zsel + 'return p.size;')],
                 'Pool size.', units='B', tag='zfs', extra_tags=ZT),
        dep_item('PBS: ZFS pool [{#POOL}]: Allocated', 'pbs.zfs.alloc[{#POOL}]', ZF, [js(zsel + 'return p.alloc;')],
                 'Allocated space.', units='B', tag='zfs', extra_tags=ZT),
        dep_item('PBS: ZFS pool [{#POOL}]: Utilization', 'pbs.zfs.pused[{#POOL}]', ZF,
                 [js(zsel + 'return p.size > 0 ? p.alloc / p.size * 100 : 0;')],
                 'Allocated space in %. ZFS performance degrades above ~80%.', 'FLOAT', '%', 'zfs',
                 extra_tags=ZT, trigger_prototypes=[trig(
                     f'last(/{T}/pbs.zfs.pused[{{#POOL}}])>{{$PBS.ZFS.PUSED.MAX}}',
                     'PBS: ZFS pool [{#POOL}]: High utilization (over {$PBS.ZFS.PUSED.MAX}%)', 'WARNING',
                     'ZFS pool is filling up; performance degrades on almost full pools.',
                     opdata='Used: {ITEM.LASTVALUE1}', uid_key='zfs-pused')]),
        dep_item('PBS: ZFS pool [{#POOL}]: Fragmentation', 'pbs.zfs.frag[{#POOL}]', ZF,
                 [js(zsel + "if (p.frag === undefined) throw 'no data';\nreturn p.frag;")],
                 'Pool free space fragmentation.', 'FLOAT', '%', 'zfs', extra_tags=ZT),
    ],
    'master_item': {'key': ZF},
    'lld_macro_paths': [{'lld_macro': '{#POOL}', 'path': '$.name'}],
})

# --------------------------------------------------------------------------------------------
macros = [
    ('{$PBS.SCHEME}', 'https', 'API scheme.'),
    ('{$PBS.PORT}', '8007', 'API port.'),
    ('{$PBS.NODE}', 'localhost', 'Node name used in /nodes/<node>/ API paths. "localhost" always works.'),
    ('{$PBS.TOKEN.ID}', 'zabbix@pbs!monitoring', 'API token ID: <user>@<realm>!<token name>.'),
    ('{$PBS.TOKEN.SECRET}', '', 'API token secret (UUID).', 'SECRET_TEXT'),
    ('{$PBS.TIMEOUT}', '15s', 'Timeout of HTTP requests.'),
    ('{$PBS.LLD.LIFETIME}', '7d', 'How long to keep resources that are no longer discovered.'),
    ('{$PBS.CPU.UTIL.CRIT}', '90', 'CPU utilization threshold, %.'),
    ('{$PBS.CPU.IOWAIT.MAX}', '30', 'CPU iowait threshold, %.'),
    ('{$PBS.LOAD.PER.CPU.MAX}', '1.5', 'Load average per CPU threshold.'),
    ('{$PBS.MEMORY.PUSED.MAX}', '90', 'Memory utilization threshold, %.'),
    ('{$PBS.ROOTFS.PUSED.MAX}', '90', 'Root filesystem utilization threshold, %.'),
    ('{$PBS.CERT.EXPIRY.WARN}', '14', 'Certificate expiry warning, days.'),
    ('{$PBS.TASKS.INTERVAL}', '5m', 'How often to read the task list.'),
    ('{$PBS.TASKS.PERIOD}', '24', 'Period for task counters and failed task triggers, hours.'),
    ('{$PBS.TASKS.LIMIT}', '5000', 'Max number of newest tasks requested from the API. Must cover all tasks '
                                    'within {$PBS.TASKS.PERIOD}.'),
    ('{$PBS.DATASTORE.MATCHES}', '.*', 'Datastores to discover (regex).'),
    ('{$PBS.DATASTORE.NOT_MATCHES}', 'CHANGE_IF_NEEDED', 'Datastores to skip (regex).'),
    ('{$PBS.DATASTORE.PUSED.WARN}', '80', 'Datastore usage warning threshold, %. Can be set per datastore '
                                          'with context: {$PBS.DATASTORE.PUSED.WARN:"store1"}.'),
    ('{$PBS.DATASTORE.PUSED.CRIT}', '90', 'Datastore usage critical threshold, %. Supports context.'),
    ('{$PBS.DATASTORE.FULL.DAYS}', '30', 'Warn when the datastore is estimated to be full within N days. '
                                         'Supports context.'),
    ('{$PBS.GROUP.INTERVAL}', '15m', 'How often to check backup group freshness.'),
    ('{$PBS.GROUP.MATCHES}', '.*', 'Backup groups to discover, regex on "<store>/<ns>/<type>/<id>", '
                                   'e.g. "store1/vm/100".'),
    ('{$PBS.GROUP.NOT_MATCHES}', 'CHANGE_IF_NEEDED', 'Backup groups to skip (regex).'),
    ('{$PBS.GROUP.IGNORE.COMMENT}', '^nomon', 'Groups whose PBS comment matches this regex get no '
                                             'freshness triggers.'),
    ('{$PBS.GROUP.AGE.WARN}', '26', 'Warn when the newest snapshot of a group is older than N hours. '
                                    'Context: {$PBS.GROUP.AGE.WARN:"vm/100"}.'),
    ('{$PBS.GROUP.AGE.CRIT}', '50', 'High severity when the newest snapshot is older than N hours. '
                                    'Supports context.'),
    ('{$PBS.JOBS.INTERVAL}', '5m', 'How often to read sync/verify/prune/GC/tape job status.'),
    ('{$PBS.JOB.MATCHES}', '.*', 'Jobs to discover (regex on job ID; for GC it is the datastore name).'),
    ('{$PBS.JOB.NOT_MATCHES}', 'CHANGE_IF_NEEDED', 'Jobs to skip (regex).'),
    ('{$PBS.JOB.MAX.AGE}', '48', 'Warn when a scheduled job has not run for N hours.'),
    ('{$PBS.JOB.MAX.AGE:"verify"}', '192', 'Max age for verify jobs (weekly by default), hours.'),
    ('{$PBS.JOB.MAX.AGE:"tape"}', '192', 'Max age for tape backup jobs, hours.'),
    ('{$PBS.DISK.MATCHES}', '.*', 'Disks to discover (regex).'),
    ('{$PBS.DISK.NOT_MATCHES}', '^(zd|loop|ram|rbd)', 'Disks to skip (regex).'),
    ('{$PBS.DISK.WEAROUT.MAX}', '80', 'SSD wearout threshold, %.'),
    ('{$PBS.ZFS.PUSED.MAX}', '80', 'ZFS pool utilization threshold, %.'),
]

valuemaps = [
    {'uuid': uid('vm', 'api'), 'name': 'PBS API state',
     'mappings': [{'value': '0', 'newvalue': 'Unavailable'}, {'value': '1', 'newvalue': 'Available'}]},
    {'uuid': uid('vm', 'job'), 'name': 'PBS job state',
     'mappings': [{'value': str(i), 'newvalue': n} for i, n in
                  enumerate(['OK', 'Warnings', 'Error', 'Running', 'Never run'])]},
    {'uuid': uid('vm', 'smart'), 'name': 'PBS SMART status',
     'mappings': [{'value': '0', 'newvalue': 'Passed'}, {'value': '1', 'newvalue': 'Failed'},
                  {'value': '2', 'newvalue': 'Unknown'}]},
]

DESCRIPTION = '''Proxmox Backup Server 4.x monitoring via its REST API (HTTP agent). Zabbix 7.4+ (nested LLD).

Monitors:
- API availability, version, subscription, package updates, API certificate expiry;
- CPU utilization / iowait / load, memory, swap, root filesystem, uptime;
- datastores: size, usage, forecast of full date, errors;
- freshness of every backup group (vm/ct/host) in every datastore and namespace;
- tasks for the last {$PBS.TASKS.PERIOD}h: OK / warning / failed per category (backup, restore, sync, verify, prune, gc, tape) + list of failed tasks;
- scheduled sync, verify, prune, garbage collection and tape backup jobs: last result, last/next run;
- disks: SMART status, SSD wearout; ZFS pools: health, usage, fragmentation.

Setup (PBS side):
  proxmox-backup-manager user create zabbix@pbs
  proxmox-backup-manager acl update / Audit --auth-id zabbix@pbs
  proxmox-backup-manager user generate-token zabbix@pbs monitoring
  proxmox-backup-manager acl update / Audit --auth-id 'zabbix@pbs!monitoring'
Setup (Zabbix side): link the template to a host with an interface (any type) pointing to the PBS IP/DNS,
set {$PBS.TOKEN.ID} and {$PBS.TOKEN.SECRET}. TLS certificate is not verified, self-signed certificate is OK.
'''

template = {
    'uuid': uid('template', T), 'template': T, 'name': T, 'description': DESCRIPTION,
    'groups': [{'name': 'Templates/Applications'}],
    'items': items, 'discovery_rules': discovery,
    'tags': [{'tag': 'class', 'value': 'software'}, {'tag': 'target', 'value': 'proxmox-backup-server'}],
    'macros': [dict(macro=m, value=v, description=d, **({'type': x[0]} if x else {}))
               for m, v, d, *x in macros],
    'valuemaps': valuemaps,
}
export = {'zabbix_export': {
    'version': '7.4',
    'template_groups': [{'uuid': uid('group', 'Templates/Applications'), 'name': 'Templates/Applications'}],
    'templates': [template],
    'triggers': top_triggers,
}}


class Dumper(yaml.SafeDumper):
    pass


def str_rep(d, s):
    if '\n' in s:
        return d.represent_scalar('tag:yaml.org,2002:str', s, style='|')
    return d.represent_scalar('tag:yaml.org,2002:str', s)


Dumper.add_representer(str, str_rep)
out = sys.argv[1] if len(sys.argv) > 1 else "template_pbs_http.yaml"
with open(out, 'w') as f:
    yaml.dump(export, f, Dumper=Dumper, sort_keys=False, allow_unicode=True, width=1000, indent=2)
print('written', out, 'items', len(items), 'lld', len(discovery))
