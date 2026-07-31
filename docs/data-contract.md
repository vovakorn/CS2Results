# Data Contract

Этот документ фиксирует границу между модулем данных `cs2bot.match_sources` и слоем публикации `cs2bot.main`.

## Ответственность data-layer

`cs2bot.match_sources` отвечает только за получение, нормализацию и фильтрацию результатов матчей.

Он делает:

- получает завершённые матчи из PandaScore;
- использует LiquipediaDB fallback, если включён `ENABLE_LIQUIPEDIA_FALLBACK`;
- приводит данные к `MatchNormalized`;
- заполняет `start_date`, `end_date`, `date` и `maps`, если источник отдаёт эти поля;
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
  детерминированно генерирует PNG и отправляет его со спойлером для результатов;
- возвращается к текстовой публикации при любой ошибке карточки;
- помечает конкретный канал обработанным только после успешной отправки.

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

`If-None-Match: *` и `If-Match: <etag>` не позволяют двум параллельным invocation одновременно получить один claim. Claim с истёкшим TTL можно безопасно перехватить. Старые `processed/{channel}_{source}_{match_id}.json` продолжают проверяться.

## Fallback contract

В режиме `source=auto` порядок такой:

1. PandaScore Fixtures adapter (`source=pandascore`).
2. LiquipediaDB adapter (`source=liquipedia`), если `ENABLE_LIQUIPEDIA_FALLBACK=1`.

Решение о fallback принимается после validation и freshness gate. Источники не объединяются в одном запуске. Явный выбор `pandascore` или `liquipedia` доступен для диагностики. Старые BO3.gg и HLTV адаптеры production selector не вызывает.

## Freshness contract

Свежесть источника считается по максимальной дате среди `end_date`, `date`, `start_date`.

Если последний матч старше `MAX_SOURCE_STALENESS_HOURS`, отсутствует дата или timestamp слишком далеко в будущем, production-вызов не получает эти матчи.

```text
event=source_stale
```

Для диагностики stale-ответ можно увидеть только в dry-run при `ALLOW_STALE_IN_DRY_RUN=1`.
