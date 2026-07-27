# Object Storage Lifecycle

Object Storage используется только для состояния дедупликации. Raw HTML, raw API responses и пользовательские секреты туда не пишутся.

## Ключи

Для публикаций в Telegram используется per-channel ключ:

```text
processed/{channel_id}_match_v1_{fingerprint}.json
```

Пример:

```text
processed/global_match_v1_5528f9748a200b85a2dfdc2b.json
```

Перед отправкой создаётся атомарный lease:

```text
claims/{channel_id}_match_v1_{fingerprint}.json
```

Старые source-specific ключи продолжают читаться для безопасной миграции. Поле `id` в `CHANNELS_JSON` должно быть стабильным и уникальным.

## IAM

Сервисному аккаунту Cloud Function нужны минимальные права на bucket:

- `head_object` / чтение метаданных;
- `put_object` / запись объекта, включая conditional `If-None-Match` и `If-Match`;
- опционально просмотр объектов для ручной диагностики.

Ключи доступа не должны храниться в коде. Используйте только переменные окружения:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
OBJECT_STORAGE_BUCKET
OBJECT_STORAGE_ENDPOINT
```

## Lifecycle policy

Рекомендуется удалять `processed/*` через 180-365 дней, а `claims/*` через 7-30 дней.

Для MVP это безопасно, потому что:

- матч не должен повторно публиковаться спустя месяцы;
- объекты маленькие, но со временем их станет много;
- стоимость будет низкой, но Object Storage остаётся платным ресурсом.

Практическая рекомендация:

- development bucket: 30-90 дней;
- production bucket: 180-365 дней;
- prefix: `processed/`.

## Ошибки и повторы

Claim закрывает гонку между параллельными invocation. Если отправка не удалась, claim немедленно освобождается. Если Telegram принял сообщение, но процесс аварийно завершился до записи `processed`, повтор после истечения lease всё ещё теоретически возможен: Telegram Bot API не предоставляет клиентский idempotency key.

Если один канал успешно отправлен, а следующий канал упал, уже успешный канал остаётся помеченным и не должен получить дубль при следующей попытке.

Payload processed-объекта содержит `start_date` и `end_date`, если источник их отдал. Это помогает ручной диагностике дублей и старых публикаций.
