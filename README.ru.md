# Шаблон Zabbix: Proxmox Backup Server by HTTP

[English](README.md) | **Русский**

Шаблон Zabbix для мониторинга **Proxmox Backup Server 4.x** через REST API (HTTP agent).
Zabbix-агент не нужен, на PBS ничего ставить не надо.

- Zabbix: **7.4+** (используется вложенный LLD)
- PBS: 4.x (проверено на ответах API PBS 4.2)
- Файл: [`template_pbs_http.yaml`](template_pbs_http.yaml)

## Что мониторится

| Раздел | Подробности |
|---|---|
| **Задачи** (за последние 24 часа, настраивается) | Счётчики OK / с предупреждениями / упавших по категориям: backup, restore, sync, verify, prune, GC, tape, прочие. Текстовый список упавших задач с текстом ошибки. Триггеры на упавшие backup, restore и прочие задачи |
| **Джобы PBS по расписанию** (sync, verify, prune, garbage collection, tape backup) | Обнаруживаются автоматически. Результат последнего запуска (OK / предупреждения / ошибка / выполняется / ни разу не запускался), время последнего и следующего запуска, pending bytes у GC. Триггеры: последний запуск упал, завершился с предупреждениями, давно не запускался |
| **Группы бэкапов** (`vm/<id>`, `ct/<id>`, `host/<имя>` во всех датасторах и namespace) | Возраст и время последнего снапшота, число снапшотов. Триггер, если нового бэкапа нет 26 ч (warning) / 50 ч (high). Ловит задания бэкапа, которые тихо перестали запускаться на стороне PVE |
| **Датасторы** | Общий / занятый / свободный объём, заполнение в %, прогноз PBS даты заполнения, ошибки датастора (например, съёмный датастор не смонтирован) |
| **Система** | Загрузка CPU, iowait, load average на ядро, память, swap, корневая ФС, uptime и перезагрузка, ядро, модель CPU |
| **Диски** | SMART-статус, износ SSD |
| **ZFS** | Состояние пула, размер, заполнение, фрагментация |
| **Прочее** | Доступность API, версия PBS, статус подписки, доступные обновления пакетов (триггер по умолчанию выключен), срок действия сертификата API |

Почти все триггеры зависят от `PBS: API is unavailable`: если PBS недоступен, придёт одна проблема, а не десятки.

## Как это работает

Каждый раздел API запрашивается одним «сырым» HTTP-item'ом (без истории). Зависимые item'ы достают
значения из этого JSON через JSONPath / JavaScript, поэтому один запрос даёт много метрик.

| Raw-item | Endpoint API | Интервал |
|---|---|---|
| Get node status | `/nodes/localhost/status` | 1m |
| Get tasks | `/nodes/localhost/tasks` | 5m |
| Get datastore usage | `/status/datastore-usage` | 5m |
| Get sync / verify / prune / GC / tape job status | `/admin/sync`, `/admin/verify`, `/admin/prune`, `/admin/gc`, `/tape/backup` | 5m |
| Get disks | `/nodes/localhost/disks/list` | 1h |
| Get ZFS pools | `/nodes/localhost/disks/zfs` | 5m |
| Версия, подписка, обновления, сертификат | `/version`, `/nodes/localhost/subscription`, `/nodes/localhost/apt/update`, `/nodes/localhost/certificates/info` | 1m–6h |

Группы бэкапов обнаруживаются трёхуровневым вложенным LLD:

```
датасторы (из datastore-usage)
  └─ namespace каждого датастора        /admin/datastore/<store>/namespace   (1h)
       └─ группы бэкапов в namespace     /admin/datastore/<store>/groups      (1h)
            └─ по группе: последний снапшот -> возраст -> триггеры
```

Каждая группа опрашивается своим лёгким запросом раз в 15 минут (`{$PBS.GROUP.INTERVAL}`):
Zabbix не разрешает зависимые item'ы между уровнями вложенного LLD.

> Задания бэкапа, настроенные в **Proxmox VE** (Datacenter → Backup), — это объекты PVE, в API PBS их нет.
> Они покрываются косвенно: упавшими задачами backup и свежестью групп бэкапов.

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
| `{$PBS.TOKEN.ID}` | `zabbix@pbs!monitoring` | ID API-токена |
| `{$PBS.TOKEN.SECRET}` | | Секрет API-токена |
| `{$PBS.TIMEOUT}` | `15s` | Таймаут HTTP-запросов |
| `{$PBS.LLD.LIFETIME}` | `7d` | Сколько хранить пропавшие объекты |
| `{$PBS.CPU.UTIL.CRIT}` | `90` | Загрузка CPU, % |
| `{$PBS.CPU.IOWAIT.MAX}` | `30` | iowait, % |
| `{$PBS.LOAD.PER.CPU.MAX}` | `1.5` | Load average на ядро |
| `{$PBS.MEMORY.PUSED.MAX}` | `90` | Использование памяти, % |
| `{$PBS.ROOTFS.PUSED.MAX}` | `90` | Заполнение корневой ФС, % |
| `{$PBS.CERT.EXPIRY.WARN}` | `14` | Предупреждение об истечении сертификата, дней |
| `{$PBS.TASKS.INTERVAL}` | `5m` | Интервал опроса списка задач |
| `{$PBS.TASKS.PERIOD}` | `24` | Окно счётчиков задач, часов |
| `{$PBS.TASKS.LIMIT}` | `5000` | Сколько последних задач запрашивать (должно покрывать окно) |
| `{$PBS.DATASTORE.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Фильтр датасторов (regex) |
| `{$PBS.DATASTORE.PUSED.WARN}` | `80` | Заполнение датастора, warning, %. Контекст: `{$PBS.DATASTORE.PUSED.WARN:"store1"}` |
| `{$PBS.DATASTORE.PUSED.CRIT}` | `90` | Заполнение датастора, high, %. Поддерживает контекст |
| `{$PBS.DATASTORE.FULL.DAYS}` | `30` | Алерт, если по прогнозу заполнится раньше чем через N дней. Поддерживает контекст |
| `{$PBS.GROUP.INTERVAL}` | `15m` | Интервал опроса групп бэкапов |
| `{$PBS.GROUP.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Фильтр групп, regex по `<store>/<ns>/<type>/<id>`, например `store1/vm/100` |
| `{$PBS.GROUP.IGNORE.COMMENT}` | `^nomon` | Группы с подходящим комментарием в PBS не получают триггеров свежести |
| `{$PBS.GROUP.AGE.WARN}` | `26` | Нет нового бэкапа, warning, часов. Контекст: `{$PBS.GROUP.AGE.WARN:"vm/100"}` |
| `{$PBS.GROUP.AGE.CRIT}` | `50` | Нет нового бэкапа, high, часов. Поддерживает контекст |
| `{$PBS.JOBS.INTERVAL}` | `5m` | Интервал опроса статуса джобов |
| `{$PBS.JOB.MATCHES}` / `NOT_MATCHES` | `.*` / `CHANGE_IF_NEEDED` | Фильтр джобов (ID джоба; для GC — имя датастора) |
| `{$PBS.JOB.MAX.AGE}` | `48` | Джоб по расписанию не запускался N часов. Для контекстов `verify` и `tape` — `192` |
| `{$PBS.DISK.MATCHES}` / `NOT_MATCHES` | `.*` / `^(zd\|loop\|ram\|rbd)` | Фильтр дисков |
| `{$PBS.DISK.WEAROUT.MAX}` | `80` | Износ SSD, % |
| `{$PBS.ZFS.PUSED.MAX}` | `80` | Заполнение ZFS-пула, % |

## Советы

- **Еженедельные бэкапы:** подними пороги возраста для группы, например `{$PBS.GROUP.AGE.WARN:"vm/100"}` = `170`.
- **Удалённые ВМ/CT, бэкапы которых хранятся:** напиши `nomon` в начале комментария группы в PBS
  или исключи их через `{$PBS.GROUP.NOT_MATCHES}`.
- **Много групп бэкапов:** увеличь `{$PBS.GROUP.INTERVAL}`, чтобы уменьшить число запросов.
- У отмонтированного съёмного датастора в Zabbix будет ошибка LLD на поиске namespace. Это нормально:
  проблему сообщает триггер `Datastore reports an error`.

## Пересборка шаблона

YAML генерируется скриптом [`tools/gen_template.py`](tools/gen_template.py) (Python 3 + PyYAML).
Правь генератор, а не YAML:

```bash
python3 tools/gen_template.py template_pbs_http.yaml
```

## Благодарности

Шаблон создан с помощью [Claude Code](https://claude.com/claude-code) (Anthropic).

## Лицензия

MIT
