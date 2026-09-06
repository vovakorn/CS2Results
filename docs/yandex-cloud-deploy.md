# Yandex Cloud Functions Deploy

Документ описывает ручной production-like deploy без Terraform. Если проект вырастет, эти шаги можно перенести в IaC.

## 1. Bucket состояния

Создайте Object Storage bucket, например:

```text
cs2-results-state
```

Включите lifecycle policy для `processed/`. Не удаляйте весь prefix `claims/`
безусловно: `attempting` и `uncertain` должны сохраняться до ручного разрешения.
Рекомендации описаны в `docs/object-storage-lifecycle.md`.

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

Если исходящий доступ Cloud Functions к Telegram недоступен, используйте
доступный HTTP(S)-прокси и храните его URL в Lockbox. Скрипт безопасного
деплоя добавляет или обновляет только ссылку на секрет:

```bash
YC_FUNCTION_ID=<function_id> \
YC_FUNCTION_PACKAGE_BUCKET=<private_package_bucket> \
YC_TELEGRAM_PROXY_SECRET_ID=<lockbox_secret_id> \
YC_TELEGRAM_PROXY_SECRET_VERSION_ID=<pinned_version_id> \
scripts/deploy_yandex_function.sh candidate

YC_FUNCTION_ID=<function_id> \
YC_PROMOTE_APPROVED=1 \
scripts/deploy_yandex_function.sh promote dist/releases/<candidate_version_id>.json
```

Значение `TELEGRAM_PROXY_URL` не передавайте в командной строке и не кладите в
обычные environment variables.

Отдельная функция `cs2-social-oauth` использует `SOCIAL_PROXY_URL` для запросов
к Instagram и Threads, если прямой доступ к Meta недоступен. URL с логином и
паролем храните отдельным секретом Lockbox и подключайте к версии OAuth-функции
как secret environment variable. Прокси применяется только к Meta API; запись
полученных токенов в Yandex Lockbox выполняется напрямую.

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

Прямая загрузка архива в Cloud Functions ограничена 3,5 МБ. Deploy-скрипт
измеряет готовый ZIP до обращения к Cloud Functions и для архива больше
3 500 000 байт требует `YC_FUNCTION_PACKAGE_BUCKET`. Архив загружается под
content-addressed ключом `function-packages/<git_sha>/<sha256>.zip`, а его
SHA-256 передаётся Cloud Functions.

Используйте отдельный приватный bucket; публичные media bucket для этого не
подходят. Перед созданием candidate скрипт проверяет folder, anonymous access,
публичные ACL/policy и наличие lifecycle, удаляющего `function-packages/`
через срок до 30 дней (фактическое удаление выполняется асинхронно).
Шаблон рассчитан на bucket без versioning; versioned bucket скрипт отклоняет,
чтобы expiration не оставлял платные noncurrent-версии.
Шаблон правила: `infra/function_package_lifecycle.json`.
Для отдельного package bucket его можно применить один раз:

```bash
yc storage bucket update \
  --name <private_package_bucket> \
  --lifecycle-rules-from-file infra/function_package_lifecycle.json
```

Команда заменяет lifecycle-конфигурацию bucket целиком. Если bucket не выделен
только под function packages, сначала объедините правило с существующими.
Правило не затрагивает `release-manifests/`: журнал релизов сохраняется дольше
архивов. Удаление ZIP не удаляет уже созданную версию Cloud Functions; для
быстрого отката предыдущая версия удерживается тегом `rollback`.

## 5. Настройка функции

Параметры:

```text
Runtime: Python 3.12
Handler: cs2bot.main.handler
Timeout: 120 seconds (значение по умолчанию в release-скрипте)
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
Для результата Telegram PNG делает до двух безопасных попыток при
`ConnectTimeout`, затем текст отправляется один раз в том же invocation и на час
включается text-only режим. Если TCP-соединение не установилось и для текста,
claim освобождается, а результат остаётся в durable outbox. При остальных
сетевых ошибках, HTTP 5xx или невалидном ответе claim остаётся
неперехватываемым, fallback не делается и result outbox удаляется: повтор
возможен только вручную после проверки платформы.

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
- в bucket созданы канонические ключи `claims/{channel_id}_match_v1_...` и `processed/{channel_id}_match_v1_...`; перед внешним запросом claim имеет `delivery-state=attempting`, а у подтверждённой отправки — `delivery-state=sent` до записи marker;
- повторный запуск не отправляет дубль в тот же канал.

Если оба источника stale/invalid, ожидается `502` с `match_source_unavailable` и ноль публикаций. Это fail-closed поведение.

Чтобы проверить код и dry-run без переключения production-тега, используйте
candidate-режим deploy-скрипта:

```bash
YC_FUNCTION_ID=<function_id> \
YC_FUNCTION_PACKAGE_BUCKET=<private_package_bucket> \
scripts/deploy_yandex_function.sh candidate
```

Он один раз собирает архив, создаёт новую версию с тегом `candidate`, выполняет
dry-run, проверяет startup-ошибки в логах и сохраняет release manifest в
`dist/releases/<candidate_version_id>.json` и `release-manifests/` package bucket.
Таймеры и `production` не меняются. Candidate требует чистого Git working tree.
Smoke ограничен локальными тайм-аутами: по умолчанию 150 секунд на invocation и
30 секунд на чтение логов; при превышении candidate считается не прошедшим.

## 7. Timer triggers

Создайте пять timer trigger. Расписание Yandex Cloud задаётся в UTC; Москва
круглый год использует UTC+3.

Получение новых результатов — каждые 15 минут:

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

Повтор outbox — каждые 5 минут. Этот trigger не вызывает PandaScore или
Liquipedia shadow; claims и processed markers предотвращают параллельные дубли:

```text
0/5 * ? * * *
```

```json
{
  "job": "results",
  "retry_only": true,
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

Поиск турниров для радара — каждый день в 09:00 UTC (12:00 МСК):

```text
0 9 ? * * *
```

```json
{
  "job": "radar_discovery",
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

Все пять заданий используют атомарную дедупликацию. Обычный `results` сохраняет
нормализованные матчи в durable outbox, а `retry_only` обрабатывает эту очередь
без повторного запроса источников. Расписание и итог получают отдельный ключ на
календарный день и канал. Пустой выпуск не отправляется и не помечается
обработанным.

## 8. Rollback

Release manifest хранит ID предыдущей и новой production-версии. Для
проверяемого rollback выполните одну команду:

```bash
YC_FUNCTION_ID=<function_id> \
YC_ROLLBACK_APPROVED=1 \
scripts/deploy_yandex_function.sh rollback dist/releases/<candidate_version_id>.json
```

Команда отказывается работать, если текущий production не совпадает с candidate
или предыдущей версией из manifest. Второй случай позволяет повторить проверку
после прерванного rollback. После переноса тега она повторно проверяет тег, все пять timer
trigger, production dry-run и startup-ошибки предыдущей версии. Реальная
Telegram-публикация не выполняется.
При откате на старый код, созданный до исправления dry-run алертов, сбой
источника всё ещё может вызвать административный алерт этой старой версии.

## 9. Deploy script

Скрипт использует версию с тегом `production` как единственный источник настроек.
Он переносит runtime, handler, память, service account, concurrency,
обычные переменные окружения, параметры логирования и закреплённые ссылки
Lockbox. Значения секретов скрипт не читает и не выводит. Создание candidate и
promote разделены: после проверки candidate production продвигается строго по
его manifest без повторной сборки и создания версии.
Timeout по существующему production-профилю скрипта — `120s`; его можно
изменить при создании candidate через `YC_EXECUTION_TIMEOUT`.

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
обязательные закреплённые привязки Lockbox и теги таймеров. Точное число
привязок зависит от включённых источников; их значения команда не читает. Код и
облачные ресурсы она не меняет.

### Candidate

Создайте и проверьте release candidate:

```bash
YC_FUNCTION_ID=<function_id> \
YC_FUNCTION_PACKAGE_BUCKET=<private_package_bucket> \
scripts/deploy_yandex_function.sh candidate
```

Последовательность candidate:

1. Проверка чистоты Git working tree и однократная сборка ZIP.
2. Проверка размера и SHA-256 до обращения к Cloud Functions.
3. Проверка приватности package bucket и lifecycle.
4. Read-only проверка production-конфигурации и всех пяти таймеров, затем
   загрузка архива и создание версии с тегом `candidate`.
5. Вызов candidate с `dry_run=true` и анализ startup-ошибок в логах.
6. Сохранение manifest с Git SHA, SHA-256 архива, package object и ID обеих
   версий.

### Promote

После проверки manifest и явного утверждения production-релиза:

```bash
YC_FUNCTION_ID=<function_id> \
YC_PROMOTE_APPROVED=1 \
scripts/deploy_yandex_function.sh promote dist/releases/<candidate_version_id>.json
```

`promote` проверяет, что production не изменился после создания candidate, а
тег `candidate` всё ещё указывает на версию из manifest. Затем он закрепляет
старую production-версию тегом `rollback`, переносит `production` на candidate и
автоматически выполняет post-deploy smoke. При неуспехе production возвращается
на предыдущую версию, а rollback проверяется тем же dry-run и анализом логов.

Post-deploy smoke можно повторить отдельно:

```bash
YC_FUNCTION_ID=<function_id> \
scripts/deploy_yandex_function.sh smoke dist/releases/<candidate_version_id>.json
```

Успех: production-тег и все пять timer trigger проверены, `statusCode=200`, тело
содержит `dry_run=true`, в логах новой версии нет startup/import/runtime ошибок.
Отправки и запись production-состояния не выполняются.

Настраиваемые параметры:

```text
YC_FOLDER_ID=<expected_folder_id>
YC_PRODUCTION_TAG=production
YC_CANDIDATE_TAG=candidate
YC_ROLLBACK_TAG=rollback
YC_DRY_RUN_PAYLOAD={"limit":1,"dry_run":true}
YC_FUNCTION_PACKAGE_BUCKET=<private_package_bucket>
YC_DIRECT_UPLOAD_MAX_BYTES=3500000
YC_EXPECTED_TRIGGER_COUNT=5
YC_PACKAGE_LIFECYCLE_MAX_DAYS=30
YC_RELEASE_DIR=dist/releases
```

Память и service account копируются из действующей production-версии.
Manifest содержит исходный smoke payload и результаты проверок без ответов
функции, сырых логов или значений секретов. В payload нельзя класть секреты.
Теги из manifest должны совпадать с настройками команды; смена локального
`YC_DRY_RUN_PAYLOAD` не меняет запрос проверенного релиза.

Команды одного checkout сериализуются локальным lock в `dist/release-locks/`.
Релизы с разных компьютеров пока нужно выполнять последовательно: повторное
чтение тега обнаруживает изменения, но облачный `set-tag` не предоставляет
скрипту условную запись по ожидаемой старой версии. Прерванный релиз имеет
сохранённый manifest; после сверки тега используйте `smoke` или `rollback`.
Один снимок startup-логов после паузы (по умолчанию 5 секунд,
`YC_SMOKE_LOG_WAIT_SECONDS`) не заменяет дальнейшее наблюдение: задержавшиеся
записи или ошибки под нагрузкой могут появиться позже. Непрочитанные логи
считаются неуспехом smoke.
