# Yandex Cloud Functions Deploy

Документ описывает ручной production-like deploy без Terraform. Если проект вырастет, эти шаги можно перенести в IaC.

## 1. Bucket состояния

Создайте Object Storage bucket, например:

```text
cs2-results-state
```

Включите lifecycle policy для prefix `processed/` и `claims/`. Рекомендации описаны в `docs/object-storage-lifecycle.md`.

## 2. Сервисный аккаунт

Создайте сервисный аккаунт для Cloud Function и выдайте ему минимальные права на bucket состояния.

Создайте static access key и передайте значения в переменные окружения функции:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
OBJECT_STORAGE_BUCKET
OBJECT_STORAGE_ENDPOINT=https://storage.yandexcloud.net
```

Не делайте функцию публичной. Право invocation должно быть только у timer trigger и назначенного service account. Ограничьте доступ к настройкам версии функции, поскольку они содержат секретные переменные окружения.

## 3. Telegram

Создайте бота через BotFather и добавьте его в канал.

Для одного канала достаточно:

```text
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=@your_channel
TELEGRAM_ADMIN_CHAT_ID=private_chat_id
TELEGRAM_SPOILERS=1
TELEGRAM_MEDIA_CARDS=0
```

Для нескольких каналов используйте:

```text
CHANNELS_JSON=[{"id":"global","name":"global","chat_id":"@cs2_results","teams":null}]
```

`id` используется в ключах дедупликации и не должен меняться при переименовании канала.

## 4. Сборка архива

Из корня репозитория:

```bash
scripts/build_function_zip.sh
```

Скрипт создаёт `dist/function.zip` и не включает `.venv`, `.git`, `.pytest_cache` и локальные секреты.
Архив содержит исходники и `requirements.txt`; зависимости устанавливаются в Linux-среде Cloud Functions, поэтому локальные platform-specific wheels в него не попадают.

## 5. Настройка функции

Параметры:

```text
Runtime: Python 3.12
Handler: cs2bot.main.handler
Timeout: 30-60 seconds
Memory: 256-512 MB
```

Базовые env vars:

```text
MATCH_SOURCE=auto
PANDASCORE_API_TOKEN=...
LIQUIPEDIA_API_KEY=<Lockbox binding>
ENABLE_LIQUIPEDIA_FALLBACK=0
ENABLE_LIQUIPEDIA_SHADOW=1
REQUEST_TIMEOUT_SECONDS=15
BOT_MODE=production
TIER1_PRIZE_POOL_THRESHOLD_USD=500000
MAX_SOURCE_STALENESS_HOURS=48
MAX_SOURCE_FUTURE_SKEW_HOURS=6
DELIVERY_CLAIM_TTL_SECONDS=300
ALERT_COOLDOWN_SECONDS=21600
DISPLAY_TIMEZONE=Europe/Moscow
MAX_SOURCE_RESPONSE_BYTES=5000000
TELEGRAM_MEDIA_CARDS=0
```

Для первого теста оставьте `TELEGRAM_MEDIA_CARDS=0`. После проверки новой
версии функции включите `TELEGRAM_MEDIA_CARDS=1`: расписание, отдельные
результаты и вечерний итог начнут приходить квадратными карточками. Изображения
с результатами отправляются с Telegram-спойлером. При определённом отклонении
карточки Telegram API функция автоматически отправит прежний текстовый формат.
При сетевой ошибке, HTTP 5xx или невалидном ответе Telegram fallback не делается:
исход доставки неизвестен, и повтор мог бы создать дубль.

Храните API-токены в Lockbox и подключайте их к версии функции как секреты. Если Lockbox пока не используется, ограничьте доступ к чтению и редактированию версии функции и не передавайте секреты в event payload.

Для Liquipedia используйте отдельный Lockbox secret и shadow-режим до принятия
решения о fallback. Точные поля, права и команда первого deploy описаны в
[`docs/liquipedia-shadow.md`](liquipedia-shadow.md).

Если нужно подменить whitelist без изменения Python-кода, положите новый JSON-файл в архив и задайте:

```text
TIER1_FILTER_CONFIG_PATH=tier1_filter.json
```

Для турниров, у которых онлайн-стадия и LAN-финалы используют общее название,
задавайте доверенную пару «турнир → фаза», а не добавляйте весь турнир в
`trusted_lan_tournament_patterns`:

```json
{
  "trusted_lan_tournament_phase_patterns": {
    "BLAST Bounty": ["Finals", "Playoffs"]
  },
  "trusted_online_tier1_tournament_phase_patterns": {
    "BLAST Bounty": ["Online Stage"]
  }
}
```

Так LAN-финалы и явно выбранная онлайн-стадия BLAST Bounty пройдут фильтр,
а остальные онлайн-турниры останутся заблокированы.

## 6. Проверка

Сначала запустите функцию вручную:

```json
{
  "limit": 10,
  "source": "auto",
  "dry_run": true,
  "include_filtered": true,
  "mode": "debug"
}
```

Ожидаемый результат:

- `statusCode` равен `200`;
- `matches_received` заполнен;
- `messages_sent` в dry-run отражает потенциальные публикации;
- объектов в Object Storage не создаётся.

Затем запустите без `dry_run` и проверьте:

- сообщение появилось в Telegram;
- в bucket созданы канонические ключи `claims/{channel_id}_match_v1_...` и `processed/{channel_id}_match_v1_...`; у подтверждённой отправки claim имеет `delivery-state=sent` до записи marker;
- повторный запуск не отправляет дубль в тот же канал.

Если оба источника stale/invalid, ожидается `502` с `match_source_unavailable` и ноль публикаций. Это fail-closed поведение.

## 7. Timer triggers

Создайте три timer trigger. Расписание Yandex Cloud задаётся в UTC; Москва
круглый год использует UTC+3.

Результаты — каждые 15 минут:

```text
0/15 * ? * * *
```

```json
{
  "job": "results",
  "limit": 30,
  "source": "auto",
  "mode": "production"
}
```

Утреннее расписание — каждый день в 07:00 UTC (10:00 МСК):

```text
0 7 ? * * *
```

```json
{
  "job": "schedule",
  "source": "pandascore",
  "mode": "production"
}
```

Вечерний итог — каждый день в 20:00 UTC (23:00 МСК):

```text
0 20 ? * * *
```

```json
{
  "job": "digest",
  "source": "pandascore",
  "mode": "production"
}
```

Все три задания используют атомарную дедупликацию. Расписание и итог получают
отдельный ключ на календарный день и канал. Пустой выпуск не отправляется и не
помечается обработанным.

## 8. Rollback

Deploy-скрипт выводит ID предыдущей и новой production-версии. Для rollback
перенесите стабильный тег на предыдущую версию:

```bash
yc serverless function version set-tag --id <previous_version_id> --tag production
```

После rollback проверьте:

- доступ к Object Storage;
- отсутствие дублей;
- Telegram отправку в dry-run и production режимах.

## 9. Deploy script

Скрипт использует версию с тегом `production` как единственный источник настроек.
Он переносит runtime, handler, память, timeout, service account, concurrency,
обычные переменные окружения, параметры логирования и закреплённые ссылки
Lockbox. Значения секретов скрипт не читает и не выводит.

### Одноразовая подготовка

Новая версия автоматически получает `$latest`. Поэтому все production-триггеры
должны вызывать стабильный тег `production`; иначе непроверенная версия может
начать получать задания сразу после создания.

Сначала назначьте тег текущей рабочей версии:

```bash
yc serverless function version set-tag \
  --id <current_version_id> \
  --tag production
```

Затем переведите каждый timer trigger на этот тег:

```bash
yc serverless trigger update timer <trigger_name> \
  --new-invoke-function-tag production
```

Скрипт проверяет это условие и отказывается создавать новую версию, если хотя бы
один trigger функции по-прежнему использует `$latest`.

### Read-only проверка

```bash
YC_FUNCTION_ID=<function_id> scripts/deploy_yandex_function.sh check
```

Команда проверяет каталог, production-тег, обязательные environment variables,
четыре закреплённые привязки Lockbox и теги таймеров. Код и облачные ресурсы она
не меняет.

### Deploy

Только после явного утверждения production-релиза:

```bash
YC_FUNCTION_ID=<function_id> \
YC_DEPLOY_APPROVED=1 \
scripts/deploy_yandex_function.sh deploy
```

Последовательность безопасного deploy:

1. Повторная read-only проверка production-конфигурации и таймеров.
2. Сборка ZIP.
3. Создание версии с тегом `candidate` и полной копией настроек.
4. Вызов `candidate` с `dry_run=true`.
5. Перенос тега `production` только при `statusCode=200` и подтверждённом
   `dry_run=true`.
6. Проверка, что production-тег указывает на новую версию.

Настраиваемые параметры:

```text
YC_FOLDER_ID=<expected_folder_id>
YC_PRODUCTION_TAG=production
YC_CANDIDATE_TAG=candidate
YC_DRY_RUN_PAYLOAD={"limit":1,"dry_run":true}
```

Память, timeout и service account вручную не задаются: они копируются из
действующей production-версии, что исключает случайное расхождение конфигурации.
