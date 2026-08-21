# Data Contract

Этот документ фиксирует границу между модулем данных `cs2bot.match_sources` и слоем публикации `cs2bot.main`.

## Ответственность data-layer

`cs2bot.match_sources` отвечает только за получение, нормализацию и фильтрацию результатов матчей.

Он делает:

- получает завершённые матчи из PandaScore;
- использует LiquipediaDB fallback, если включён `ENABLE_LIQUIPEDIA_FALLBACK`;
- приводит данные к `MatchNormalized`;
- заполняет `start_date`, `end_date`, `date` и `maps`, если источник отдаёт эти поля;
- сохраняет provider-scoped ID лиги, серии, турнира, команд и победителя в
  `source_refs`;
- сохраняет `tournament_tier`, `rescheduled`, `original_scheduled_at` и `forfeit`,
  если PandaScore отдаёт эти поля;
- сохраняет для Liquipedia карты, `best of`, tier и publisher tier, стадию,
  точность даты, VOD и статусы технического результата, если они доступны;
- для явно отмеченного финала сопоставляет победителя с первым местом турнира и
  сохраняет `winner_prize_usd`, только если LiquipediaDB отдал однозначную сумму;
- сохраняет необязательные `team1_logo_url` и `team2_logo_url`, если PandaScore
  отдаёт изображения команд;
- отбрасывает невалидные матчи;
- применяет Tier-1 LAN фильтр;
- логирует свежесть источника через `source_fresh`, `source_stale`, `source_future_timestamp` или `source_freshness_unknown`;
- не возвращает stale/undated данные для production-публикации;
- проверяет дедупликацию, если вызывающий слой просит `check_processed=True`.

Он не делает:

- не отправляет сообщения в Telegram;
- не генерирует карточки;
- не помечает матч обработанным после публикации;
- не знает о конкретных Telegram-каналах, кроме CLI debug-режима с `--channel`.

## Ответственность delivery-layer

`cs2bot.main` отвечает за Cloud Functions handler и публикацию.

Он делает:

- получает матчи через `get_new_finished_matches(..., check_processed=False)`;
- применяет фильтры каналов по командам;
- атомарно резервирует per-channel доставку через conditional Object Storage write;
- проверяет новые канонические и старые source-specific ключи дедупликации;
- отправляет сообщение в Telegram;
- при включённом `TELEGRAM_MEDIA_CARDS` безопасно загружает логотипы,
  детерминированно генерирует PNG для отдельных результатов, вечернего итога и
  расписания; карточки результатов отправляет со спойлером;
- возвращается к текстовой публикации при определённом отклонении карточки Telegram API; при сетевой ошибке, HTTP 5xx или невалидном ответе исход доставки считается неизвестным и fallback не выполняется;
- после подтверждённой отправки сохраняет в claim состояние `sent`, затем помечает конкретный канал обработанным;
- восстанавливает отсутствующий processed marker по claim со состоянием `sent`, не отправляя сообщение повторно.

## Основной объект

Все источники должны возвращать `MatchNormalized`.

Минимально пригодный матч содержит:

- `team1_name`;
- `team2_name`;
- `score1`;
- `score2`;
- `tournament_name`;
- `match_id` или `match_url`.

`maps` всегда должен быть списком. Если карты неизвестны, используется пустой список.

`team1_logo_url` и `team2_logo_url` не влияют на идентичность матча и не являются
обязательными. Delivery-layer принимает изображения только с разрешённого CDN
PandaScore; URL от других источников не загружаются.

`start_date` и `end_date` хранят исходные ISO datetime от источника. `date` остаётся совместимым полем отображения и обычно равно `end_date`, если оно известно. `competition_key` хранит стабильное название соревнования без названия стадии и используется только для cross-provider дедупликации.

`source_refs` содержит идентификаторы только в пространстве текущего `source`:
`league_id`, `serie_id`, `tournament_id`, `team1_id`, `team2_id` и
`winner_team_id`. Эти значения предназначены для будущих связей с командами,
турнирами и сетками. Они, как и `tournament_tier`, не входят в fingerprint и не
могут создавать повторные публикации при переключении источника.

`is_final` и `winner_prize_usd` — только source-confirmed поля LiquipediaDB.
Специальная карточка финала с картами и призовыми создаётся исключительно для
матча с `source=liquipedia`, явным признаком финала, суммой победителя и полным
счётом карт. Для PandaScore и неполных данных используется обычная карточка
результата без догадок.

## PandaScore tier в shadow-режиме

PandaScore tier хранится как одно из значений `s`, `a`, `b`, `c`, `d`. Если
сокращённый объект турнира внутри матча не содержит tier, адаптер одним запросом
получает все недостающие турниры через `GET /tournaments` с `filter[id]` и
кеширует результат в пределах тёплого экземпляра функции. Ошибка этого
необязательного запроса не блокирует получение матчей.

Tier пока не изменяет production-отбор. Текущие whitelist, разрешённые стадии и
исключения остаются авторитетными. Для сравнения решений пишутся агрегированные
события `pandascore_tier_diagnostics` и
`pandascore_upcoming_tier_diagnostics`; dry-run также возвращает tier рядом с
фактическим решением фильтра.

## Дедупликация

Data-layer может использовать общий ключ:

```text
processed/match_v1_{fingerprint}.json
```

Delivery-layer использует per-channel ключ:

```text
processed/{channel_id}_match_v1_{fingerprint}.json
```

Fingerprint строится из даты, `competition_key` (или отображаемого названия турнира), нормализованных псевдонимов команд и счёта. Он не зависит от source ID и порядка команд. Если даты нет, применяется старый source-specific UID, чтобы не склеивать разные матчи по недостаточным данным.

Перед отправкой создаётся lease:

```text
claims/{channel_id}_match_v1_{fingerprint}.json
```

`If-None-Match: *` и `If-Match: <etag>` не позволяют двум параллельным invocation одновременно получить один claim. У claim есть состояние доставки в metadata:

- `sending` — lease текущей попытки; после истечения `DELIVERY_CLAIM_TTL_SECONDS` его можно перехватить;
- `released` — Telegram точно не подтвердил доставку до отправки, claim можно сразу получить заново;
- `sent` — Telegram подтвердил доставку; claim хранится 24 часа, не перехватывается и служит только для восстановления отсутствующего processed marker.

При сетевой ошибке, HTTP 5xx или невалидном ответе Telegram нельзя отличить
неотправленное сообщение от уже принятого. В этом случае автоматический retry и
текстовый fallback запрещены, а claim сохраняется для диагностики. Старые
`processed/{channel}_{source}_{match_id}.json` продолжают проверяться.
После подтверждённого ответа Telegram запись состояния `sent` повторяется до трёх
раз при временной ошибке Object Storage; окончательная ошибка поднимает отдельный
алерт и не освобождает claim.

## Fallback contract

В режиме `source=auto` порядок такой:

1. PandaScore Fixtures adapter (`source=pandascore`).
2. LiquipediaDB adapter (`source=liquipedia`), если `ENABLE_LIQUIPEDIA_FALLBACK=1`.

Решение о fallback принимается после validation и freshness gate. Источники не объединяются в одном запуске. Явный выбор `pandascore` или `liquipedia` доступен для диагностики. Старые BO3.gg и HLTV адаптеры production selector не вызывает.

## Liquipedia shadow contract

`ENABLE_LIQUIPEDIA_SHADOW=1` добавляет fail-open сравнение после успешного ответа
PandaScore. Сопоставление использует дату и нормализованную пару команд, поэтому
порядок соперников у провайдеров не важен. Сравниваются счёт, `best of` и tier;
отдельно измеряется покрытие карт и технических результатов.

Shadow-ответ не объединяется с PandaScore, не участвует в фильтрации,
дедупликации и публикации. Любая ошибка Liquipedia записывается как
`liquipedia_shadow_failed`, после чего основной поток продолжает работу.

## Freshness contract

Свежесть источника считается по максимальной дате среди `end_date`, `date`, `start_date`.

Если последний матч старше `MAX_SOURCE_STALENESS_HOURS`, отсутствует дата или timestamp слишком далеко в будущем, production-вызов не получает эти матчи.

```text
event=source_stale
```

Для диагностики stale-ответ можно увидеть только в dry-run при `ALLOW_STALE_IN_DRY_RUN=1`.
