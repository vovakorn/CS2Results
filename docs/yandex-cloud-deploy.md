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
ENABLE_HLTV_FALLBACK=1
REQUEST_TIMEOUT_SECONDS=15
BOT_MODE=production
TIER1_PRIZE_POOL_THRESHOLD_USD=500000
MAX_SOURCE_STALENESS_HOURS=48
MAX_SOURCE_FUTURE_SKEW_HOURS=6
DELIVERY_CLAIM_TTL_SECONDS=300
MAX_SOURCE_RESPONSE_BYTES=5000000
```

Если нужно подменить whitelist без изменения Python-кода, положите новый JSON-файл в архив и задайте:

```text
TIER1_FILTER_CONFIG_PATH=tier1_filter.json
```

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

## 7. Timer trigger

Создайте timer trigger с периодом 60 минут.

Рекомендуемое событие:

```json
{
  "limit": 30,
  "source": "auto",
  "mode": "production"
}
```

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
