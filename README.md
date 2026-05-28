# CS2 Results Bot

Телеграм-бот и MVP-модуль данных для получения результатов завершённых матчей CS2.

Модуль `cs2bot.match_sources` получает нормализованные результаты из бесплатного основного источника `cs2api` / BO3.gg и использует `https://www.hltv.org/results` как HTTP fallback. Handler Cloud Functions использует этот data-layer, отправляет новые матчи в Telegram и помечает их обработанными только после успешной публикации.

## Переменные окружения
| Название | Описание |
| --- | --- |
| `TELEGRAM_TOKEN` или `TELEGRAM_BOT_TOKEN` | Токен вашего Telegram-бота, полученный у BotFather. |
| `TELEGRAM_CHAT_ID` | ID канала или чата, куда будут отправляться результаты. |
| `AWS_ACCESS_KEY_ID` | Access key сервисного аккаунта для Object Storage. |
| `AWS_SECRET_ACCESS_KEY` | Secret key сервисного аккаунта для Object Storage. |
| `OBJECT_STORAGE_BUCKET` | Bucket для дедупликации обработанных матчей. |
| `OBJECT_STORAGE_ENDPOINT` | Endpoint Object Storage, по умолчанию `https://storage.yandexcloud.net`. |
| `CHANNELS_JSON` | JSON-массив каналов, если нужно больше одного канала или фильтр по командам. |
| `BOT_MODE` | `production` или `debug`. В `debug` можно видеть отфильтрованные матчи. |
| `TIER1_FILTER_CONFIG_JSON` | JSON-конфиг Tier-1 LAN фильтра без изменения кода. |

Опционально:

```text
MATCH_SOURCE=auto
ENABLE_HLTV_FALLBACK=1
REQUEST_TIMEOUT_SECONDS=15
DISPLAY_TIMEZONE=Europe/Berlin
BOT_MODE=production
TIER1_PRIZE_POOL_THRESHOLD_USD=500000
```

Пример `CHANNELS_JSON`:

```json
[
  {"name": "global", "chat_id": "@cs2_results", "teams": null},
  {"name": "navi", "chat_id": "@navi_results", "teams": ["NAVI", "Natus Vincere"]}
]
```

Пример `TIER1_FILTER_CONFIG_JSON` можно взять из `tier1_filter.example.json`:

```json
{
  "known_operators": ["ESL", "IEM", "PGL", "BLAST"],
  "tournament_patterns": ["IEM", "ESL Pro League", "Major"],
  "online_location_markers": ["online", "remote"],
  "prize_pool_threshold_usd": 500000
}
```

## Локальная проверка match_sources

Установите зависимости:

```bash
pip install -r requirements.txt
```

Dry-run без записи в Object Storage:

```bash
python -m cs2bot.match_sources.match_fetcher --source auto --limit 30 --dry-run
```

Посмотреть также матчи, отброшенные Tier-1 LAN фильтром:

```bash
python -m cs2bot.match_sources.match_fetcher --source auto --limit 30 --dry-run --include-filtered
```

Выбор источника:

```bash
python -m cs2bot.match_sources.match_fetcher --source auto --limit 10 --dry-run
python -m cs2bot.match_sources.match_fetcher --source cs2api --limit 10 --dry-run
python -m cs2bot.match_sources.match_fetcher --source hltv --limit 10 --dry-run
```

`get_new_finished_matches` только возвращает новые матчи. В Cloud Functions обработчик помечает матч обработанным после успешной отправки в Telegram. Для ручной отладки есть отдельный флаг:

```bash
python -m cs2bot.match_sources.match_fetcher --source auto --limit 10 --mark-processed
```

Дедупликация хранит каждую публикацию отдельным объектом. Это важно для нескольких каналов: один и тот же матч может быть опубликован в `global` и в командный канал, но не будет повторён в одном и том же канале.

```text
processed/{channel}_{source}_{match_id}.json
```

HLTV fallback в MVP не использует Playwright, Selenium или Camoufox. Если HLTV отдаёт 403 или меняет HTML, ошибка логируется контролируемо. Browser fallback оставлен как отдельный placeholder `hltv_browser_source.py`; если он понадобится, его лучше запускать отдельным контейнерным сервисом, а не внутри Cloud Functions.

## Режимы работы

- `production`: публикуются только матчи, прошедшие Tier-1 LAN фильтр.
- `debug`: можно включить `include_filtered`, чтобы увидеть матчи с `filter_reason`.
- `dry_run`: ничего не отправляет в Telegram и не пишет в Object Storage.

Пример события для ручного запуска Cloud Function:

```json
{
  "limit": 10,
  "source": "auto",
  "dry_run": true,
  "include_filtered": true,
  "mode": "debug"
}
```

## Источники данных

Primary source `cs2api` использует два адаптера:

1. установленную библиотеку `cs2api`;
2. прямой HTTP-запрос к BO3.gg API, если библиотека недоступна, изменила интерфейс или вернула пустой результат.

Если оба адаптера не дали данные, `auto` переключается на HLTV HTTP fallback. HLTV может вернуть `403`, поэтому browser fallback вынесен в отдельный placeholder и не добавлен в зависимости Cloud Functions.

## Логи, метрики и алерты

Код пишет structured JSON logs через стандартный `logging`. Основные события:

- `handler_start`
- `handler_complete`
- `fetch_failed`
- `publish_failed`
- `duplicate_skipped`

Ответ handler содержит поле `metrics`:

```json
{
  "matches_received": 3,
  "messages_sent": 1,
  "duplicates_skipped": 0,
  "filtered_skipped": 2,
  "channels": {"global": 1}
}
```

Рекомендуемые алерты в Yandex Cloud Logging / Monitoring:

- `fetch_failed > 0` за последние 2-3 запуска;
- `publish_failed > 0`;
- `messages_sent = 0` долгое время при наличии `matches_received > 0`;
- рост ошибок Object Storage;
- частый `HLTV returned HTTP 403`, если fallback становится критичным.

## Структура проекта
```
cs2bot/
    __init__.py
    config.py        # конфигурация каналов
    hltv_api.py      # legacy adapter, deprecated
    main.py          # handler(event, context)
    match_sources/   # MVP-модуль нормализованных источников матчей
        match_fetcher.py
        models.py
        filters.py
        storage.py
        sources/
requirements.txt     # зависимости (requests)
runtime.txt          # целевой Python runtime
tier1_filter.example.json
```

## Тесты

```bash
pytest
```

## Развёртывание в Yandex Cloud Functions

1. Создайте Object Storage bucket для состояния, например `cs2-results-state`.
2. Настройте lifecycle policy для удаления старых объектов `processed/*` через 180-365 дней.
3. Создайте сервисный аккаунт с минимальными правами чтения/записи в этот bucket.
4. Создайте static access key для сервисного аккаунта.
5. Соберите зависимости:

```bash
pip install -r requirements.txt -t packages/
```

6. Упакуйте `cs2bot/` и `packages/` в архив.
7. Загрузите архив в Cloud Functions с runtime Python 3.11+.
8. Укажите handler:

```text
cs2bot.main.handler
```

9. Задайте env vars:

```text
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
OBJECT_STORAGE_BUCKET=cs2-results-state
OBJECT_STORAGE_ENDPOINT=https://storage.yandexcloud.net
MATCH_SOURCE=auto
BOT_MODE=production
```

10. Создайте timer trigger с периодом 60 минут.
11. Для проверки запустите функцию вручную с `dry_run=true`.
12. После проверки отключите `dry_run` и проверьте, что объекты появляются по ключам `processed/{channel}_{source}_{match_id}.json`.
