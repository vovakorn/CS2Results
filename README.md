# CS2 Results Bot

Телеграм-бот и MVP-модуль данных для получения результатов завершённых матчей CS2.

Модуль `cs2bot.match_sources` получает нормализованные результаты из документированного PandaScore API и использует одобренный LiquipediaDB API как резервный и диагностический источник. В shadow-режиме Liquipedia сравнивается с PandaScore, но не меняет публикацию. Fallback включается отдельно только при пустом, невалидном, недоступном или устаревшем ответе PandaScore. Перед публикацией handler проверяет качество и свежесть данных, резервирует доставку атомарным Object Storage claim и помечает её завершённой только после успешной отправки.

## Переменные окружения
| Название | Описание |
| --- | --- |
| `TELEGRAM_TOKEN` или `TELEGRAM_BOT_TOKEN` | Токен вашего Telegram-бота, полученный у BotFather. |
| `TELEGRAM_CHAT_ID` | ID канала или чата, куда будут отправляться результаты. |
| `TELEGRAM_ADMIN_CHAT_ID` | Приватный chat ID администратора для технических алертов. Не указывайте публичный канал. |
| `TELEGRAM_SPOILERS` | `1` скрывает счёт и победителя Telegram-спойлером; по умолчанию `1`. |
| `TELEGRAM_MEDIA_CARDS` | `1` включает брендированные PNG-карточки для расписания и результатов; по умолчанию `0` для безопасного поэтапного запуска. |
| `AWS_ACCESS_KEY_ID` | Access key сервисного аккаунта для Object Storage. |
| `AWS_SECRET_ACCESS_KEY` | Secret key сервисного аккаунта для Object Storage. |
| `OBJECT_STORAGE_BUCKET` | Bucket для дедупликации обработанных матчей. |
| `OBJECT_STORAGE_ENDPOINT` | Endpoint Object Storage, по умолчанию `https://storage.yandexcloud.net`. |
| `CHANNELS_JSON` | JSON-массив каналов. Поле `id` — стабильный идентификатор дедупликации, который не следует менять при переименовании канала. |
| `BOT_MODE` | `production` или `debug`. Отфильтрованные матчи доступны только вместе с `dry_run=true`. |
| `TIER1_FILTER_CONFIG_JSON` | JSON-конфиг Tier-1 LAN фильтра без изменения кода. |
| `TIER1_FILTER_CONFIG_PATH` | Путь к JSON-файлу Tier-1 LAN фильтра, по умолчанию `tier1_filter.json`. |
| `PANDASCORE_API_TOKEN` | Токен PandaScore Fixtures API. Обязателен для основного источника. |
| `LIQUIPEDIA_API_KEY` или `LPDB_API_KEY` | Ключ LiquipediaDB API, выдаваемый после одобрения заявки. |
| `ENABLE_LIQUIPEDIA_FALLBACK` | `1` включает Liquipedia fallback в режиме `auto`, `0` отключает его. |
| `ENABLE_LIQUIPEDIA_SHADOW` | `1` параллельно сравнивает завершённые матчи PandaScore и Liquipedia, не меняя источник публикации; по умолчанию `0`. |
| `DISPLAY_TIMEZONE` | Таймзона для отображения ISO datetime в Telegram, по умолчанию `Europe/Moscow`. |
| `MAX_SOURCE_STALENESS_HOURS` | Максимальный возраст источника для production-публикации, по умолчанию `48`. |
| `MAX_SOURCE_FUTURE_SKEW_HOURS` | Допустимое отклонение даты источника в будущее, по умолчанию `6`. |
| `ALLOW_STALE_IN_DRY_RUN` | Разрешает показывать stale-данные только в dry-run, по умолчанию `1`. |
| `DELIVERY_CLAIM_TTL_SECONDS` | Срок lease атомарного delivery claim, по умолчанию `300`. |
| `ALERT_COOLDOWN_SECONDS` | Минимальный интервал между одинаковыми алертами, по умолчанию `21600` (6 часов). |
| `MAX_SOURCE_RESPONSE_BYTES` | Максимальный размер ответа внешнего источника, по умолчанию `5000000`. |

Опционально:

```text
MATCH_SOURCE=auto
ENABLE_LIQUIPEDIA_FALLBACK=0
ENABLE_LIQUIPEDIA_SHADOW=1
REQUEST_TIMEOUT_SECONDS=15
DISPLAY_TIMEZONE=Europe/Moscow
TELEGRAM_SPOILERS=1
TELEGRAM_MEDIA_CARDS=0
BOT_MODE=production
TIER1_PRIZE_POOL_THRESHOLD_USD=500000
MAX_SOURCE_STALENESS_HOURS=48
MAX_SOURCE_FUTURE_SKEW_HOURS=6
DELIVERY_CLAIM_TTL_SECONDS=300
ALERT_COOLDOWN_SECONDS=21600
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
  "featured_tier2_tournament_patterns": ["CCT", "Thunderpick World Championship", "BetBoom Dacha"],
  "online_location_markers": ["online", "remote"],
  "trusted_lan_tournament_patterns": ["Major", "IEM Cologne", "IEM Katowice"],
  "trusted_lan_tournament_phase_patterns": {"BLAST Bounty": ["Finals", "Playoffs"]},
  "trusted_online_tier1_tournament_phase_patterns": {"BLAST Bounty": ["Online Stage"]},
  "tournament_exclusion_patterns": ["qualifier", "showmatch", "academy league"],
  "team_exclusion_patterns": ["academy", "youth", "junior"],
  "popular_teams": ["NAVI", "Team Spirit", "Vitality", "MOUZ", "FaZe", "G2"],
  "team_aliases": {"natus vincere": "navi", "navi": "navi"},
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
python -m cs2bot.match_sources.match_fetcher --source pandascore --limit 10 --dry-run
python -m cs2bot.match_sources.match_fetcher --source liquipedia --limit 10 --dry-run
```

`get_new_finished_matches` только возвращает новые матчи. В Cloud Functions обработчик помечает матч обработанным после успешной отправки в Telegram. Для ручной отладки есть отдельный флаг:

```bash
python -m cs2bot.match_sources.match_fetcher --source auto --limit 10 --channel global --mark-processed
```

`--mark-processed` требует хотя бы один `--channel`, чтобы CLI использовал те же per-channel ключи, что и Telegram handler. `--channel` можно повторять.

Дедупликация использует source-independent fingerprint, `competition_key` и нормализованные псевдонимы команд. Поэтому переключение PandaScore ↔ Liquipedia не должно повторно публиковать тот же матч. Для совместимости также проверяются старые source-specific ключи.

```text
claims/{channel_id}_match_v1_{fingerprint}.json
processed/{channel_id}_match_v1_{fingerprint}.json
```

Старые адаптеры BO3.gg и HLTV сохранены только для миграционных тестов и диагностики. Production selector их не вызывает.

## Режимы работы

- `production`: публикуются только матчи, прошедшие Tier-1 LAN фильтр.
- `debug`: можно включить `include_filtered`, чтобы увидеть матчи с `filter_reason`, но только при `dry_run=true`.
- `dry_run`: ничего не отправляет в Telegram и не пишет в Object Storage.
- `job=results`: новые подтверждённые Tier-1 LAN результаты.
- `job=schedule`: один утренний выпуск с Tier-1 матчами и матчами популярных команд.
- `job=digest`: один вечерний выпуск с итогами Tier-1 LAN; пустой выпуск не публикуется.

При `TELEGRAM_MEDIA_CARDS=1` результаты отправляются как квадратные PNG со
спойлером на всём изображении, а счёт и победитель в подписи остаются скрыты
через `<tg-spoiler>`. Вечерний итог и расписание поддерживают квадратные
адаптивные карточки от одного до десяти матчей. Во всех шаблонах логотип канала
расположен по центру верхней части карточки. Если логотип команды отсутствует,
используется аккуратный плейсхолдер с инициалами. Если карточку нельзя
сформировать или доставить, бот автоматически возвращается к существующему
текстовому сообщению и не теряет матч.

Логотипы загружаются только по HTTPS с `cdn.pandascore.co`, без редиректов, с
ограничением типа, размера ответа, разрешения изображения и таймаутом. Шрифты
Russo One и Rajdhani поставляются вместе с функцией по лицензии OFL.

Для турниров со смешанным форматом можно доверять только конкретной LAN-фазе.
Например, `{"BLAST Bounty": ["Finals", "Playoffs"]}` пропускает LAN-финалы
(`Playoffs` — название этой стадии в PandaScore), но не считает
предшествующую онлайн-стадию LAN-турниром. Отдельный параметр
`trusted_online_tier1_tournament_phase_patterns` позволяет явно публиковать
выбранные онлайн-стадии Tier-1 событий, не открывая остальные онлайн-турниры.
Если матч выглядит как Tier-1, но не относится ни к подтверждённой LAN-фазе,
ни к настроенному онлайн-исключению, публичная отправка блокируется, а
администратор получает ограниченный по частоте технический алерт.

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

Primary source `pandascore` использует документированный endpoint `GET /csgo/matches/past` и передаёт токен в заголовке `Authorization`. Адаптер принимает только записи со статусом `finished`. Валидатор блокирует `0:0`, ничьи без подтверждённого победителя и невозможный итог BO1/BO3/BO5. Бесплатный Fixtures-план предоставляет итог серии, команды и турнир; карты не являются обязательной частью MVP.

Адаптер сохраняет provider-scoped ID лиги, серии, турнира, команд и победителя,
а также tier турнира и признаки переноса/технического результата. Если tier не
пришёл внутри матча, недостающие турниры обогащаются одним кешируемым запросом
`GET /tournaments?filter[id]=...`. Tier работает в shadow-режиме: виден в
dry-run и агрегированной диагностике, но пока не меняет production-фильтр.

Для утреннего расписания используется документированный endpoint `GET /csgo/matches/upcoming`. Окно запроса соответствует текущему календарному дню в `DISPLAY_TIMEZONE`. В расписание попадают Tier-1 турниры, а также матчи популярных команд на явно перечисленных крупных Tier-2 турнирах; qualifiers, showmatch, мелкие турниры и молодёжные составы исключаются. Списки популярных команд и заметных Tier-2 турниров настраиваются через `popular_teams` и `featured_tier2_tournament_patterns`.

Если PandaScore вернул пустые, невалидные, недатированные или устаревшие данные, `auto` переключается на одобренный LiquipediaDB API при `ENABLE_LIQUIPEDIA_FALLBACK=1`. Liquipedia вызывается через `https://api.liquipedia.net/api/v3/match` с `Authorization: Apikey ...`. Если ни один источник не прошёл freshness gate, production handler возвращает контролируемую ошибку и ничего не публикует.

При `ENABLE_LIQUIPEDIA_SHADOW=1` и наличии ключа тот же endpoint вызывается после
успешного PandaScore. Пишется только агрегированное событие
`liquipedia_shadow_comparison`: покрытие, совпадения, расхождения счёта, `best of`,
tier, наличие карт и технических результатов. Ошибка shadow-запроса не блокирует
PandaScore и Telegram; dry-run возвращает те же агрегаты в поле
`liquipedia_shadow`. Настройка ключа описана в
[`docs/liquipedia-shadow.md`](docs/liquipedia-shadow.md).

`competition_key` и конфиг `team_aliases` сводят различающиеся названия турниров и команд к общему fingerprint. Liquipedia-сообщения содержат атрибуцию и ссылку на исходную страницу. Ключи обоих API нельзя передавать в event payload или сохранять в репозитории.

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
- частый переход `fallback=liquipedia`, если основной источник становится нестабильным.

При настроенном `TELEGRAM_ADMIN_CHAT_ID` критические ошибки источника, конфигурации и доставки также отправляются администратору. Одинаковые сообщения ограничиваются одним алертом на шестичасовое окно через Object Storage. Устаревание источника более чем на `MAX_SOURCE_STALENESS_HOURS` считается его недоступностью и вызывает тот же алерт.

## Зафиксированный охват первого канала

Первая production-версия публикует только результаты Tier-1 LAN:

- основной турнир, без open/closed/regional qualifiers и showmatch;
- без academy, youth и junior составов;
- только завершённая серия с подтверждённым победителем;
- счёт BO1/BO3/BO5 должен соответствовать формату серии;
- счёт и победитель по умолчанию скрыты Telegram-спойлером.

Пауза между крупными LAN-турнирами допустима: канал не заполняется низкокачественными результатами ради частоты. Утром публикуется расписание Tier-1 и заметных матчей популярных команд, вечером — краткий итог только при наличии завершённых Tier-1 LAN матчей.

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
PANDASCORE_API_TOKEN=...
LIQUIPEDIA_API_KEY=...
MATCH_SOURCE=auto
ENABLE_LIQUIPEDIA_FALLBACK=1
BOT_MODE=production
```

10. Создайте три timer trigger: результаты раз в 15 минут, расписание в 10:00 МСК и итоги в 23:00 МСК.
11. Для каждого `job` сначала запустите функцию вручную с `dry_run=true`.
12. После проверки отключите `dry_run` и проверьте, что объекты появляются в `claims/` и `processed/`.

Функция не должна быть публичной: право invocation выдавайте только trigger/service account. Не передавайте токены и ключи внутри event payload.

Можно собрать архив скриптом:

```bash
scripts/build_function_zip.sh
```

Не добавляйте в архив локальные `site-packages`: бинарные wheels с macOS/Windows несовместимы с Linux runtime Cloud Functions.

Безопасный deploy через `yc` выполняется только после read-only проверки:

```bash
YC_FUNCTION_ID=<function_id> scripts/deploy_yandex_function.sh check
YC_FUNCTION_ID=<function_id> YC_DEPLOY_APPROVED=1 scripts/deploy_yandex_function.sh deploy
```

Скрипт копирует конфигурацию и ссылки Lockbox из версии с тегом `production`,
проверяет candidate через `dry_run` и только затем переносит production-тег.
Таймеры должны быть заранее привязаны к тегу `production`, а не к `$latest`.

## Архитектурные контракты

Контракт между модулем данных и Telegram handler описан в `docs/data-contract.md`.

Коротко:

- `cs2bot.match_sources` получает и нормализует матчи;
- `cs2bot.main` отвечает за Telegram, каналы и per-channel дедупликацию;
- матч помечается обработанным только после успешной отправки в конкретный канал.
