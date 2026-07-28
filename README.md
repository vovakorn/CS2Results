# CS2 Results Bot

Телеграм-бот и MVP-модуль данных для получения результатов завершённых матчей CS2.

Модуль `cs2bot.match_sources` получает нормализованные результаты из бесплатного основного источника `cs2api` / BO3.gg и использует `https://www.hltv.org/results` как HTTP fallback. Перед публикацией handler проверяет валидность и свежесть источника, резервирует доставку атомарным Object Storage claim и помечает её завершённой только после успешной отправки.

## Переменные окружения
| Название | Описание |
| --- | --- |
| `TELEGRAM_TOKEN` или `TELEGRAM_BOT_TOKEN` | Токен вашего Telegram-бота, полученный у BotFather. |
| `TELEGRAM_CHAT_ID` | ID канала или чата, куда будут отправляться результаты. |
| `AWS_ACCESS_KEY_ID` | Access key сервисного аккаунта для Object Storage. |
| `AWS_SECRET_ACCESS_KEY` | Secret key сервисного аккаунта для Object Storage. |
| `OBJECT_STORAGE_BUCKET` | Bucket для дедупликации обработанных матчей. |
| `OBJECT_STORAGE_ENDPOINT` | Endpoint Object Storage, по умолчанию `https://storage.yandexcloud.net`. |
| `CHANNELS_JSON` | JSON-массив каналов. Поле `id` — стабильный идентификатор дедупликации, который не следует менять при переименовании канала. |
| `BOT_MODE` | `production` или `debug`. Отфильтрованные матчи доступны только вместе с `dry_run=true`. |
| `TIER1_FILTER_CONFIG_JSON` | JSON-конфиг Tier-1 LAN фильтра без изменения кода. |
| `TIER1_FILTER_CONFIG_PATH` | Путь к JSON-файлу Tier-1 LAN фильтра, по умолчанию `tier1_filter.json`. |
| `ENABLE_HLTV_FALLBACK` | `1` включает HLTV fallback в режиме `auto`, `0` отключает автоматический fallback. |
| `DISPLAY_TIMEZONE` | Таймзона для отображения ISO datetime в Telegram, по умолчанию `Europe/Berlin`. |
| `MAX_SOURCE_STALENESS_HOURS` | Максимальный возраст источника для production-публикации, по умолчанию `48`. |
| `MAX_SOURCE_FUTURE_SKEW_HOURS` | Допустимое отклонение даты источника в будущее, по умолчанию `6`. |
| `ALLOW_STALE_IN_DRY_RUN` | Разрешает показывать stale-данные только в dry-run, по умолчанию `1`. |
| `DELIVERY_CLAIM_TTL_SECONDS` | Срок lease атомарного delivery claim, по умолчанию `300`. |
| `MAX_SOURCE_RESPONSE_BYTES` | Максимальный размер ответа внешнего источника, по умолчанию `5000000`. |

Опционально:

```text
MATCH_SOURCE=auto
ENABLE_HLTV_FALLBACK=1
REQUEST_TIMEOUT_SECONDS=15
DISPLAY_TIMEZONE=Europe/Berlin
BOT_MODE=production
TIER1_PRIZE_POOL_THRESHOLD_USD=500000
MAX_SOURCE_STALENESS_HOURS=48
MAX_SOURCE_FUTURE_SKEW_HOURS=6
DELIVERY_CLAIM_TTL_SECONDS=300
MAX_SOURCE_RESPONSE_BYTES=5000000
```

Пример `CHANNELS_JSON`:

```json
[
  {"id": "global", "name": "global", "chat_id": "@cs2_results", "teams": null},
  {"id": "navi", "name": "NAVI", "chat_id": "@navi_results", "teams": ["NAVI", "Natus Vincere"]}
]
```

Базовый whitelist лежит в `tier1_filter.json`. Его можно обновлять без изменения Python-кода. Пример `TIER1_FILTER_CONFIG_JSON` можно взять из `tier1_filter.example.json`:

```json
{
  "tournament_patterns": ["IEM", "ESL Pro League", "Major"],
  "online_location_markers": ["online", "remote"],
  "prize_pool_threshold_usd": 500000
}
```

## Локальная проверка match_sources

Установите зависимости:

```bash
pip install -r requirements-dev.txt
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
python -m cs2bot.match_sources.match_fetcher --source auto --limit 10 --channel global --mark-processed
```

`--mark-processed` требует хотя бы один `--channel`, чтобы CLI использовал те же per-channel ключи, что и Telegram handler. `--channel` можно повторять.

Дедупликация использует source-independent fingerprint, поэтому переключение BO3.gg ↔ HLTV не должно повторно публиковать тот же матч. Для совместимости также проверяются старые source-specific ключи.

```text
claims/{channel_id}_match_v1_{fingerprint}.json
processed/{channel_id}_match_v1_{fingerprint}.json
```

HLTV fallback в MVP не использует Playwright, Selenium или Camoufox. Если HLTV отдаёт 403 или меняет HTML, ошибка логируется контролируемо. Browser fallback оставлен как отдельный placeholder `hltv_browser_source.py`; если он понадобится, его лучше запускать отдельным контейнерным сервисом, а не внутри Cloud Functions.

## Режимы работы

- `production`: публикуются только матчи, прошедшие Tier-1 LAN фильтр.
- `debug`: можно включить `include_filtered`, чтобы увидеть матчи с `filter_reason`, но только при `dry_run=true`.
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

Primary source с внутренним именем `cs2api` использует контролируемый прямой HTTP-запрос к BO3.gg API. Сторонний Python-wrapper удалён: он обращался к тому же upstream и не обеспечивал независимый fallback.

Если BO3.gg вернул пустые, невалидные, недатированные или устаревшие данные, `auto` переключается на HLTV HTTP fallback при `ENABLE_HLTV_FALLBACK=1`. Если ни один источник не прошёл freshness gate, production handler возвращает контролируемую ошибку и ничего не публикует. Явный запуск `--source hltv` доступен независимо от флага.

Для BO3.gg адаптер нормализует `start_date`, `end_date`, `date`, `tournament.prize` и карты из `games`, когда эти поля доступны. `date` остаётся совместимым полем для отображения, а `start_date` / `end_date` позволяют точнее проверять свежесть данных.

## Логи, метрики и алерты

Код пишет structured JSON logs через стандартный `logging`. Основные события:

- `handler_start`
- `handler_complete`
- `fetch_failed`
- `publish_failed`
- `duplicate_or_inflight_skipped`
- `delivery_claim_failed`
- `delivery_state_failed`
- `source_fresh`
- `source_stale`
- `source_freshness_unknown`
- `source_future_timestamp`

Ответ handler содержит поле `metrics`:

```json
{
  "matches_received": 3,
  "messages_sent": 1,
  "duplicates_skipped": 0,
  "filtered_skipped": 2,
  "delivery_failures": 0,
  "channels": {"global": 1}
}
```

Рекомендуемые алерты в Yandex Cloud Logging / Monitoring:

- `fetch_failed > 0` за последние 2-3 запуска;
- `publish_failed > 0`;
- `messages_sent = 0` долгое время при наличии `matches_received > 0`;
- рост ошибок Object Storage;
- `source_stale` или `source_future_timestamp`;
- частый `HLTV returned HTTP 403`, если fallback становится критичным.

## Структура проекта
```
docs/
    data-contract.md
    object-storage-lifecycle.md
    yandex-cloud-deploy.md
scripts/
    build_function_zip.sh
    deploy_yandex_function.sh
cs2bot/
    __init__.py
    config.py          # конфигурация Telegram и каналов
    logging_utils.py   # structured JSON logs
    main.py            # handler(event, context)
    match_sources/     # MVP-модуль нормализованных источников матчей
        match_fetcher.py
        models.py
        filters.py
        storage.py
        sources/
.github/workflows/tests.yml
requirements.txt       # зависимости
runtime.txt            # целевой Python runtime
tier1_filter.json
tier1_filter.example.json
```

## Тесты

```bash
python -m pytest
```

На GitHub добавлен workflow `.github/workflows/tests.yml`: он проверяет зависимости через `pip check` и `pip-audit`, компилирует пакет, запускает pytest на Python 3.11 и 3.12 и проверяет сборку архива. Dependabot еженедельно предлагает обновления Python-пакетов и GitHub Actions.

## Развёртывание в Yandex Cloud Functions

Подробная инструкция вынесена в `docs/yandex-cloud-deploy.md`. Ниже короткий чеклист.

1. Создайте Object Storage bucket для состояния, например `cs2-results-state`.
2. Настройте lifecycle policy для удаления старых объектов `processed/*` через 180-365 дней. Детали: `docs/object-storage-lifecycle.md`.
3. Создайте сервисный аккаунт с минимальными правами чтения/записи в этот bucket.
4. Создайте static access key для сервисного аккаунта.
5. Соберите platform-independent source archive через `scripts/build_function_zip.sh`.
6. Загрузите архив в Cloud Functions: сервис установит закреплённые зависимости из `requirements.txt`.
7. Используйте поддерживаемый runtime Python 3.12.
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
12. После проверки отключите `dry_run` и проверьте, что объекты появляются в `claims/` и `processed/`.

Функция не должна быть публичной: право invocation выдавайте только trigger/service account. Не передавайте токены и ключи внутри event payload.

Можно собрать архив скриптом:

```bash
scripts/build_function_zip.sh
```

Не добавляйте в архив локальные `site-packages`: бинарные wheels с macOS/Windows несовместимы с Linux runtime Cloud Functions.

Или создать новую версию функции через `yc`:

```bash
YC_FUNCTION_NAME=cs2-results-bot \
YC_SERVICE_ACCOUNT_ID=... \
scripts/deploy_yandex_function.sh
```

## Архитектурные контракты

Контракт между модулем данных и Telegram handler описан в `docs/data-contract.md`.

Коротко:

- `cs2bot.match_sources` получает и нормализует матчи;
- `cs2bot.main` отвечает за Telegram, каналы и per-channel дедупликацию;
- матч помечается обработанным только после успешной отправки в конкретный канал.
