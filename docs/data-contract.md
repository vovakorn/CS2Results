# Data Contract

Этот документ фиксирует границу между модулем данных `cs2bot.match_sources` и слоем публикации `cs2bot.main`.

## Ответственность data-layer

`cs2bot.match_sources` отвечает только за получение, нормализацию и фильтрацию результатов матчей.

Он делает:

- получает завершённые матчи из `cs2api` / BO3.gg;
- использует HLTV HTTP fallback, если включён `ENABLE_HLTV_FALLBACK`;
- приводит данные к `MatchNormalized`;
- заполняет `start_date`, `end_date`, `date` и `maps`, если источник отдаёт эти поля;
- отбрасывает невалидные матчи;
- применяет Tier-1 LAN фильтр;
- логирует свежесть источника через `source_fresh`, `source_stale` или `source_freshness_unknown`;
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
- проверяет per-channel дедупликацию;
- отправляет сообщение в Telegram;
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

`start_date` и `end_date` хранят исходные ISO datetime от источника. `date` остаётся совместимым полем отображения и обычно равно `end_date`, если оно известно.

## Дедупликация

Data-layer может использовать общий ключ:

```text
processed/{source}_{match_id}.json
```

Delivery-layer использует per-channel ключ:

```text
processed/{channel}_{source}_{match_id}.json
```

Это позволяет опубликовать один и тот же матч в нескольких каналах, но не повторять его в одном и том же канале.

## Fallback contract

В режиме `source=auto` порядок такой:

1. `cs2api` library adapter.
2. BO3.gg HTTP adapter.
3. HLTV HTTP fallback, если `ENABLE_HLTV_FALLBACK=1`.

Если `ENABLE_HLTV_FALLBACK=0`, явный запуск `--source hltv` всё равно разрешён, но автоматический fallback из `auto` не выполняется.

## Freshness contract

Свежесть источника считается по максимальной дате среди `end_date`, `date`, `start_date`.

Если последний матч старше `MAX_SOURCE_STALENESS_HOURS`, модуль пишет warning:

```text
event=source_stale
```

Это не блокирует публикацию само по себе, но должно использоваться для мониторинга качества источника.
