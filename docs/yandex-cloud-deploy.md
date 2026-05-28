# Yandex Cloud Functions Deploy

Документ описывает ручной production-like deploy без Terraform. Если проект вырастет, эти шаги можно перенести в IaC.

## 1. Bucket состояния

Создайте Object Storage bucket, например:

```text
cs2-results-state
```

Включите lifecycle policy для prefix `processed/`. Рекомендации описаны в `docs/object-storage-lifecycle.md`.

## 2. Сервисный аккаунт

Создайте сервисный аккаунт для Cloud Function и выдайте ему минимальные права на bucket состояния.

Создайте static access key и передайте значения в переменные окружения функции:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
OBJECT_STORAGE_BUCKET
OBJECT_STORAGE_ENDPOINT=https://storage.yandexcloud.net
```

## 3. Telegram

Создайте бота через BotFather и добавьте его в канал.

Для одного канала достаточно:

```text
TELEGRAM_TOKEN=...
TELEGRAM_CHAT_ID=@your_channel
```

Для нескольких каналов используйте:

```text
CHANNELS_JSON=[{"name":"global","chat_id":"@cs2_results","teams":null}]
```

## 4. Сборка архива

Из корня репозитория:

```bash
python -m pip install -r requirements.txt -t packages/
zip -r function.zip cs2bot packages requirements.txt runtime.txt
```

Не включайте `.venv`, `.git`, `.pytest_cache` и локальные секреты.

## 5. Настройка функции

Параметры:

```text
Runtime: Python 3.11+
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
- в bucket создан ключ `processed/{channel}_{source}_{match_id}.json`;
- повторный запуск не отправляет дубль в тот же канал.

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
