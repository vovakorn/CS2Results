# Safe zone Reels — состояние релиза 25 октября 2026

**Выпущено в production 25 октября 2026, итоговая сверка — 14:26:02 МСК.**
Все три функции ACTIVE, production dry-run — HTTP 200. Пользователь отдельно
ответил «разрешаю» на разовое исключение по недоступным startup-логам именно
для трёх кандидатов ниже. Startup-логи остаются непроверенными.

## Код и CI

- [PR #164](https://github.com/vovakorn/CS2Results/pull/164) слит в
  `21f6ee51571ce85d8eb352d6402e23c0ceee2fc4`. Интегрирован `main` с PR #163;
  календарь и остальные незавершённые checkout не перезаписывались.
- **1 218 Python / 27 Node passed** локально. Предрелизные проверки прежних
  координат обновлены с сохранением требований для квадратных карточек.
- [CI run 40184155829](https://github.com/vovakorn/CS2Results/actions/runs/40184155829):
  Python 3.11/3.12, audit/pip check, Python ZIP, Linux Node bundle,
  полное кодирование/декодирование Americas и monthly — success.

## Точные версии

| Компонент | Rollback | Production | Smoke завершён, МСК |
| --- | --- | --- | --- |
| Основной бот | `d4e723ilmnbgebj5uknm` | `d4e0ko5n3dfonjcem4f4` | 14:25:00 |
| VRS renderer | `d4e6u0tiersucqk7pb3k` | `d4er62kv5q46lc29a93n` | 14:24:07 |
| Monthly publisher | `d4en67h3m9s23og9ccnu` | `d4e4lkpierd7dq7fa7ae` | 14:24:30 |

Это исходные согласованные кандидаты, без повторного создания версий.
Все три production ACTIVE, находятся в прежних private-функциях и используют
пакеты одного слитого Git. Порядок переключения: renderer → monthly → main.
Runtime, entrypoint, ресурсы, timeout, concurrency, environment и pinned
secret references, параметры логирования и прочая конфигурация совпадают
с соответствующей предыдущей версией; проверены до и после переключения.
Предыдущие версии удерживаются тегом `rollback`. Значения Lockbox не извлекались.

Девять таймеров полностью совпали с preflight-снимком и вызывают `production`.
Радар остаётся PAUSED; новый таймер не создавался. Префиксы
`vrs-monthly/prepared/` и `vrs-monthly/pending/` пусты до и после релиза.
Claims/markers и существующие контейнеры вручную не менялись.

## Фактические облачные проверки кандидатов

- Main: import-metrics dry-run — **HTTP 200**. Отдельный render-probe
  расписания — **HTTP 200**, 20 матчей / 29 s, 2 644 592 bytes,
  60 441 ms, max RSS 119 972 KiB. Видео не загружалось в public media.
- VRS: world/monthly и Americas render-probe — **HTTP 200**,
  каждый по 1 944 кадра, 1080 × 1920 / 60 fps / 32,4 s, full decode passed,
  `layoutVersion=instagram-safe-v1`, `coverOffsetMs=500`.
- VRS live monthly source dry-run: world top-20, пять дат от 31 августа
  до 28 сентября за закрытый сентябрь 2026, **HTTP 200**.
- Monthly publisher: обычный и `inspect_source` dry-run — **HTTP 200**;
  `publishing_enabled=true`, `owner_allowed=true`, generation 0.
  Это проверка готовности, а не подтверждение социальной публикации.

Новые посты вручную не отправлялись. Все invocations были dry-run;
render-probe использовали временные файлы без записи подготовленных выпусков.

После каждого переключения проверен именно тег `production` и его version ID:
основной import-metrics dry-run, VRS monthly source за сентябрь и месячный
`inspect_source` вернули **HTTP 200 / dry_run=true**. Последний подтвердил
world top-20, пять исходных дат и `publishing_enabled=true`, `owner_allowed=true`,
generation 0. Повторная сверка подтвердила все production/rollback-теги,
неизменность конфигураций, приватность функций и полное совпадение девяти таймеров.

## Startup-log gate

Штатное чтение журналов каждого кандидата и production smoke возвращает
`rpc error: code = Unavailable desc = Unavailable`. Прямой список лог-групп
через CLI также недоступен. Проверен официальный REST
[LogGroup.List](https://yandex.cloud/en/docs/logging/api-ref/LogGroup/list):
HTTP **503**, `message=Unavailable`. Причина недоступности не установлена.

Startup-ошибки остаются **непроверенными**. Значение
`startup_error_detected=false` в исходном main manifest не служит доказательством
их отсутствия, поскольку рядом сохранён `error=log_read_failed`.
Исходные ошибки не удалялись, обычные deploy-скрипты не ослаблялись.
Дополнительный локальный runner проверен на отказ при HTTP 502,
неверном теге, false dry-run, старой версии layout, startup exception
и недоступных журналах.

После остановки строгого gate получено свежее явное разрешение «разрешаю».
Локальный одноразовый runner связывает исключение с Git, тремя function/version ID,
предыдущими версиями и SHA-256 пакетов. Разрешена только ошибка чтения
`Unavailable`: HTTP/body, dry-run, теги, конфигурации, приватность и таймеры
остались обязательными; сбой после переключения восстанавливает прежний тег.
Дополнительно проверены 12 сценариев: чужая версия, Git или hash, HTTP 502,
ошибки invocation/tag, false dry-run, startup exception и иной log error
не допускаются. Production smoke хранит `startupLogsChecked=false`,
`passed=false`, `releaseAccepted=true`, `acceptedWithLogException=true`.
Обычные deploy-скрипты не менялись; следующие релизы снова требуют обычный gate.

## Локальные артефакты

В release checkout `reels-safe-zone/CS2results`:

- `dist/releases/d4e0ko5n3dfonjcem4f4.json` — main manifest с production smoke;
- `dist/reels-safe-zone-release/main-render-probe.json`;
- `dist/reels-safe-zone-release/vrs.json`, `monthly.json`;
- `dist/reels-safe-zone-release/original-{main,vrs,monthly}-manifest.json` —
  неизменённые исходные failed/blocked manifests с исходными ошибками;
- `dist/reels-safe-zone-release/approval-2026-10-25.json`, `approved-release.json` —
  точная область разрешения, hash исходных manifests и результаты переключений;
- `dist/reels-safe-zone-release/logging-alternative-check.json`;
- `dist/reels-safe-zone-release/verification.json` — финальная сверка тегов,
  конфигураций, приватности и всех таймеров.

SHA-256 пакетов:

- Main: `23cbfc305a063322b24de44101c03ab45d50ef1e5fc849a501444d80b7e435d9`.
- VRS: `1bcb6c3a12cc37b2c61501dae2ef32e4650be2386ae2529fec7af0dfbe7c04eb`.
- Monthly: `cd3d903ef672f97f4e6cee1275d4f7d16094bc428cd9c472cc087872822ea02f`.

Полные дополнительные manifests сохранены локально: автоматическая проверка
отклонила их отправку в облако из-за operational/source-метаданных. Эта отправка
не выполнялась повторно; облачные пакеты и штатный исходный main manifest,
загруженные на этапе candidate, этим не менялись.

Проверка реального UI Instagram на телефоне и фактической сетки профиля остаётся
невыполненной: рендеры и центральные кропы проверены, ручных тестовых публикаций
не было. Версии deployment собраны из `21f6ee5`; последующее обновление
документации не требует пересборки или повторного deploy.
