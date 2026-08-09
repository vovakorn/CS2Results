# Liquipedia API: ключ и shadow-режим

Liquipedia подключается вторым, диагностическим слоем. PandaScore остаётся
источником публикаций, а ответ Liquipedia используется только для сравнения.
Ошибка, пустой или устаревший ответ Liquipedia не блокирует Telegram.

## Сохранение ключа в Yandex Lockbox

Не передавайте API key в чат, GitHub, event payload Cloud Function, обычную
environment variable или команду deploy. Рекомендуемый способ — отдельный
секрет Lockbox, чтобы не затрагивать Telegram, PandaScore и Object Storage.

1. Войдите в [Liquipedia API Dashboard](https://api.liquipedia.net/) и скопируйте
   выданный API key.
2. Откройте Yandex Cloud, каталог `b1g5j8hk4gjas2vpvgqr`, затем **Lockbox →
   Создать секрет**.
3. Заполните поля:
   - имя: `cs2-results-liquipedia-api`;
   - описание: `Liquipedia API key for CS2ResultsBot`;
   - защита от удаления: включена;
   - ключ записи: `LIQUIPEDIA_API_KEY`;
   - значение: API key из Liquipedia Dashboard.
4. В созданном секрете откройте **Права доступа → Назначить роли**.
5. Выберите service account Cloud Function `aje9frqb8ra8500290qc` и назначьте
   только роль `lockbox.payloadViewer` для этого секрета.
6. Сохраните только два несекретных идентификатора: `secret ID` и
   `current version ID`. Само значение повторно не копируйте.

Проверка метаданных через CLI не раскрывает payload:

```bash
yc lockbox secret get <LIQUIPEDIA_SECRET_ID>
yc lockbox secret list-versions <LIQUIPEDIA_SECRET_ID>
yc lockbox secret list-access-bindings <LIQUIPEDIA_SECRET_ID>
```

Не используйте `yc lockbox payload get`: для подключения функции значение ключа
читать не требуется.

## Локальный smoke test

Ключ можно временно ввести без сохранения в истории shell:

```bash
read -r -s -p "Liquipedia API key: " LIQUIPEDIA_API_KEY
printf '\n'
export LIQUIPEDIA_API_KEY
export ENABLE_LIQUIPEDIA_SHADOW=1
python -m cs2bot.match_sources.match_fetcher \
  --source pandascore \
  --limit 10 \
  --dry-run \
  --include-filtered
unset LIQUIPEDIA_API_KEY ENABLE_LIQUIPEDIA_SHADOW
```

Ожидаемое событие в логах:

```text
event=liquipedia_shadow_comparison
```

Cloud Function dry-run также возвращает объект `liquipedia_shadow` с теми же
агрегатами.

Оно содержит только агрегаты: число совпадений, пропуски, расхождения счёта,
`best of`, tier, покрытие карт и технические результаты. Ключ и полные ответы API
не логируются.

## Первый безопасный deploy

Эти параметры передают только pinned-ссылку Lockbox. Значение API key deploy-
скрипт не читает:

```bash
YC_FUNCTION_ID=d4e6e13rlrl7go01m2q2 \
YC_LIQUIPEDIA_SECRET_ID=<LIQUIPEDIA_SECRET_ID> \
YC_LIQUIPEDIA_SECRET_VERSION_ID=<LIQUIPEDIA_SECRET_VERSION_ID> \
YC_LIQUIPEDIA_SECRET_KEY=LIQUIPEDIA_API_KEY \
YC_ENABLE_LIQUIPEDIA_SHADOW=1 \
YC_DEPLOY_APPROVED=1 \
scripts/deploy_yandex_function.sh deploy
```

Команду выполняют только после утверждения release. Первый deploy добавит
`LIQUIPEDIA_API_KEY` как пятую Lockbox-привязку и
`ENABLE_LIQUIPEDIA_SHADOW=1` как обычный флаг. Следующие версии автоматически
унаследуют обе настройки из версии с тегом `production`.

Fallback остаётся выключен:

```text
ENABLE_LIQUIPEDIA_FALLBACK=0
```

После 7–14 дней сравнения можно отдельно решить, включать ли Liquipedia как
production fallback или использовать отдельные поля в публикациях.

## Атрибуция и лимиты

- Использовать только LPDB API, без HTML scraping.
- Не превышать 60 запросов в час и кешировать стабильные ответы.
- Пока данные видны только в закрытых логах shadow-режима, Telegram не меняется.
- Перед публичным использованием карт, сетки, составов или других данных рядом с
  ними должна появиться ссылка `Источник: Liquipedia`.
- Файлы и изображения Liquipedia могут иметь отдельные лицензии; этот shadow-слой
  их не скачивает.
