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
LIQUIPEDIA_API_KEY=...
ENABLE_LIQUIPEDIA_FALLBACK=1
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
версии функции включите `TELEGRAM_MEDIA_CARDS=1`: расписание начнёт приходить
вертикальной карточкой, а результаты — квадратной карточкой с Telegram-спойлером.
При проблеме с изображением функция автоматически отправит прежний текстовый
формат.

Храните API-токены в Lockbox и подключайте их к версии функции как секреты. Если Lockbox пока не используется, ограничьте доступ к чтению и редактированию версии функции и не передавайте секреты в event payload.

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
- в bucket созданы канонические ключи `claims/{channel_id}_match_v1_...` и `processed/{channel_id}_match_v1_...`;
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

Для rollback оставьте предыдущую версию функции активной или загрузите предыдущий архив `function.zip`.

После rollback проверьте:

- доступ к Object Storage;
- отсутствие дублей;
- Telegram отправку в dry-run и production режимах.

## 9. Deploy script

Если установлен и настроен `yc`, можно создать новую версию функции одной командой:

```bash
YC_FUNCTION_NAME=cs2-results-bot \
YC_SERVICE_ACCOUNT_ID=... \
scripts/deploy_yandex_function.sh
```

Опционально:

```text
YC_MEMORY=512m
YC_TIMEOUT=60s
```
