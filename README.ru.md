# Шаблон Zabbix: Proxmox Backup Server by HTTP

[English](README.md) | **Русский**

Шаблон Zabbix для мониторинга **Proxmox Backup Server 4.x** через REST API (HTTP agent).
Zabbix-агент не нужен, на PBS ничего ставить не надо.

- Zabbix: **7.4+** (используется вложенный LLD)
- PBS: 4.x (проверено на ответах API PBS 4.2)
- Файл: [`template_pbs_http.yaml`](template_pbs_http.yaml)

## Что мониторится

Шаблон намеренно лёгкий: только то, что нужно, чтобы знать, что с бэкапами всё в порядке.
На PBS с 2 датасторами, ~25 группами бэкапов и 6 джобами получается около 45 item.

| Что | Подробности |
|---|---|
| **Группы бэкапов** (`vm/<id>`, `ct/<id>`, `host/<имя>` во всех датасторах и namespace) | Возраст последнего снапшота. **Average**, если нового бэкапа нет 26 ч, **High** — после 50 ч. Ловит задания бэкапа на стороне PVE, которые упали или тихо перестали запускаться |
| **Джобы PBS по расписанию** (sync, verify, prune, garbage collection) | Обнаруживаются автоматически, называются по датастору / namespace. Результат последнего запуска. Триггеры: последний запуск упал (текст ошибки в названии проблемы), завершился с предупреждениями |
| **Датасторы** | Заполнение в % и свободное место. Триггеры на 80% / 90% |
| **Система** | Загрузка CPU, заполнение корневой ФС |
| **API** | Доступность. Остальные триггеры зависят от `PBS: API is unavailable`, поэтому при недоступности PBS приходит одна проблема, а не десятки |

## Как это работает

| Item | Эндпоинт API | Интервал |
|---|---|---|
| API availability | `/version` | 1m |
| Get node status (CPU, корневая ФС) | `/nodes/localhost/status` | 1m |
| Get datastore usage | `/status/datastore-usage` | 5m |
| Get sync / verify / prune / GC job status | `/admin/sync`, `/admin/verify`, `/admin/prune`, `/admin/gc` | 5m |

Item «Get ...» не хранят историю: зависимые item берут значения из их JSON.

Группы бэкапов обнаруживаются вложенным LLD в 3 уровня:

```
датасторы (из datastore-usage)
  └─ namespace каждого датастора        /admin/datastore/<store>/namespace   (1h)
       └─ группы бэкапов namespace      /admin/datastore/<store>/groups      (1h)
            └─ на группу: один item «Last backup age» + 2 триггера
```

Каждая группа — один HTTP item, опрашивается раз в 15 минут (`{$PBS.GROUP.INTERVAL}`),
потому что Zabbix не разрешает зависимые item между уровнями вложенного LLD.

> Задания бэкапа, настроенные в **Proxmox VE** (Datacenter → Backup), — объекты PVE, в API PBS их не видно.
> Они контролируются через свежесть групп бэкапов.

## Установка

### 1. PBS: создать API-токен только на чтение

```bash
proxmox-backup-manager user create zabbix@pbs
proxmox-backup-manager acl update / Audit --auth-id zabbix@pbs
proxmox-backup-manager user generate-token zabbix@pbs monitoring
proxmox-backup-manager acl update / Audit --auth-id 'zabbix@pbs!monitoring'
```

Роль `Audit` нужна **и** пользователю, **и** токену (у токенов разделение привилегий).
Сохрани секрет, который выведет `generate-token`.

### 2. Zabbix

1. *Сбор данных → Шаблоны → Импорт* → `template_pbs_http.yaml`.
2. Создай хост и прикрепи шаблон **Proxmox Backup Server by HTTP**.
3. Добавь интерфейс любого типа (например, Agent) с IP или DNS-именем PBS. URL собирается из `{HOST.CONN}`.
4. Задай макросы на хосте:

| Макрос | Значение |
|---|---|
| `{$PBS.TOKEN.ID}` | `zabbix@pbs!monitoring` (по умолчанию) |
| `{$PBS.TOKEN.SECRET}` | секрет токена, тип **Secret text** |

TLS-сертификат не проверяется, поэтому стандартный самоподписанный сертификат PBS подходит.

## Макросы

| Макрос | По умолчанию | Описание |
|---|---|---|
| `{$PBS.SCHEME}` | `https` | Схема API |
| `{$PBS.PORT}` | `8007` | Порт API |
| `{$PBS.NODE}` | `localhost` | Имя ноды в путях `/nodes/<node>/` |
| `{$PBS.TOKEN.ID}` | `zabbix@pbs!monitoring` | ID токена API |
| `{$PBS.TOKEN.SECRET}` | | Секрет токена API |
| `{$PBS.TIMEOUT}` | `15s` | Таймаут HTTP |
| `{$PBS.LLD.LIFETIME}` | `7d` | Сколько хранить пропавшие объекты |
| `{$PBS.CPU.UTIL.CRIT}` | `90` | Загрузка CPU, % |
| `{$PBS.ROOTFS.PUSED.MAX}` | `90` | Заполнение корневой ФС, % |
| `{$PBS.DATASTORE.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Фильтр датасторов (regex) |
| `{$PBS.DATASTORE.PUSED.WARN}` | `80` | Заполнение датастора, warning, %. Контекст: `{$PBS.DATASTORE.PUSED.WARN:"store1"}` |
| `{$PBS.DATASTORE.PUSED.CRIT}` | `90` | Заполнение датастора, high, %. Поддерживает контекст |
| `{$PBS.GROUP.INTERVAL}` | `15m` | Интервал опроса групп бэкапов |
| `{$PBS.GROUP.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Фильтр групп, regex по `<store>/<ns>/<type>/<id>`, например `store1/vm/100` |
| `{$PBS.GROUP.IGNORE.COMMENT}` | `^nomon` | Для групп с таким комментарием в PBS триггеры свежести не создаются |
| `{$PBS.GROUP.AGE.WARN}` | `26` | Нет нового бэкапа, average, часов. Контекст: `{$PBS.GROUP.AGE.WARN:"vm/100"}` |
| `{$PBS.GROUP.AGE.CRIT}` | `50` | Нет нового бэкапа, high, часов. Поддерживает контекст |
| `{$PBS.JOBS.INTERVAL}` | `5m` | Интервал опроса джоб |
| `{$PBS.JOB.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Фильтр джоб (ID джобы; для GC — имя датастора) |

## Советы

- **Еженедельные бэкапы:** подними пороги возраста для группы, например `{$PBS.GROUP.AGE.WARN:"vm/100"}` = `170`.
- **Удалённые ВМ/CT, бэкапы которых хранятся:** напиши `nomon` в начале комментария группы в PBS
  или исключи их через `{$PBS.GROUP.NOT_MATCHES}`.
- **Много групп бэкапов:** увеличь `{$PBS.GROUP.INTERVAL}`, чтобы уменьшить число запросов.

## Благодарности

Шаблон создан с помощью [Claude Code](https://claude.com/claude-code) (Anthropic).

## Лицензия

MIT
