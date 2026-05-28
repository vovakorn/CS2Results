# Object Storage Lifecycle

Object Storage используется только для состояния дедупликации. Raw HTML, raw API responses и пользовательские секреты туда не пишутся.

## Ключи

Для публикаций в Telegram используется per-channel ключ:

```text
processed/{channel}_{source}_{match_id}.json
```

Пример:

```text
processed/global_hltv_2378481.json
processed/navi_cs2api_984321.json
```

CLI без Telegram может использовать общий data-layer ключ только для низкоуровневой отладки:

```text
processed/{source}_{match_id}.json
```

Для проверки именно Telegram-сценария используйте CLI с `--channel`.

## IAM

Сервисному аккаунту Cloud Function нужны минимальные права на bucket:

- `head_object` / чтение метаданных;
- `put_object` / запись объекта;
- опционально просмотр объектов для ручной диагностики.

Ключи доступа не должны храниться в коде. Используйте только переменные окружения:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
OBJECT_STORAGE_BUCKET
OBJECT_STORAGE_ENDPOINT
```

## Lifecycle policy

Рекомендуется удалять старые объекты `processed/*` через 180-365 дней.

Для MVP это безопасно, потому что:

- матч не должен повторно публиковаться спустя месяцы;
- объекты маленькие, но со временем их станет много;
- стоимость будет низкой, но Object Storage остаётся платным ресурсом.

Практическая рекомендация:

- development bucket: 30-90 дней;
- production bucket: 180-365 дней;
- prefix: `processed/`.

## Ошибки и повторы

Если Telegram-отправка успешна, но запись в Object Storage упала, при следующем запуске возможен повтор. Поэтому handler помечает канал сразу после успешной отправки в этот канал и возвращает ошибку, если mark operation не удалась.

Если один канал успешно отправлен, а следующий канал упал, уже успешный канал остаётся помеченным и не должен получить дубль при следующей попытке.
