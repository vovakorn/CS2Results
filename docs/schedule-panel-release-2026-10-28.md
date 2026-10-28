# Релиз подложек и первого экрана Reel — 28 октября 2026

Позднее пользователь добавил в состав выпуска новую центровку из
«А · Предложение». Candidate ниже её не содержит и больше не соответствует
согласованному составу; его не продвигать. [Продолжение выпуска](schedule-alignment-release-2026-10-28.md).

Пользователь разрешил выпуск сообщением «Заливай» после итоговой сводки.
Согласован основной центрированный вариант с панелями и общей рамкой:
полное название PGL, официальный знак справа и большой фоновый знак сохранены.

## Состав

- Тематические панели расписания, контекста и Reel вместо старой синей базы.
- Новая шапка intro, отдельные плашки всех уникальных команд, до шести на
  странице. Максимум — 20 матчей / 40 команд / 38 секунд.
- Отступ турнирного знака от края, ослабленные диагонали Reel и улучшенный
  контраст текста. Полное WCAG AA не заявляется.
- Регрессионные проверки, документация и offline-генераторы сравнения.

Выравнивание матчевых строк остаётся предложением. Варианты без подложек,
Safety Zone/Align Left и сетка 3×3 не включены в рабочий renderer.

## Предрелизная проверка

- `origin/main` сверён: `54c912cc430071b2d2045d3f5f8dbe49447e68d4`.
- Штатный read-only `check` успешен: production `d4e40kgvrkgv4s1p7rnj`,
  30 environment entries, 7 закреплённых Lockbox references и семь main timers.
- Полный локальный набор: **1 384 passed, 1 failed** за 268,01 s. Единственная
  ошибка — запрет sandbox на bind `127.0.0.1` в `test_available_loopback_port_is_bindable`.
  Этот же тест без sandbox прошёл: **1 passed**. Код не менялся; полный CI
  на Linux остаётся обязательным перед merge.
- `git diff --check` успешен. История визуальных проверок и ограничений:
  [сводка](work-summary-2026-10-28.md), [аудит](reel-accessibility-2026-10-28.md).

## GitHub

[PR #169](https://github.com/vovakorn/CS2Results/pull/169) создан из отдельной
ветки `codex/schedule-panel-reel-20261028`, слит в
`884364d8b39f41c0aa6cc61a07e1f7d83e8444fc` 28 октября в 14:52:36 МСК.
[Итоговый CI](https://github.com/vovakorn/CS2Results/actions/runs/40682642575)
успешен: **1 385 Python-тестов** на каждой версии 3.11/3.12, **27 Node-тестов**,
dependency consistency/audit, сборки ZIP и Linux render/decode Americas/monthly.
Нерешённых review threads перед merge не было.

## Точный кандидат и облачная проверка

- Создан один candidate **`d4ep6970qo8ifcva2k8f`**, ACTIVE, из чистого merge SHA.
- Основная private-функция: `d4e6e13rlrl7go01m2q2`.
- Предыдущая и пока действующая production: **`d4e40kgvrkgv4s1p7rnj`**.
- ZIP с Xray: 17 196 970 bytes, SHA-256
  `13d3319b5a7440dd1aff188730c5da54854c8931564e35d90eb80fb6bb134063`.
- Candidate smoke: HTTP 200, `dry_run=true`; чтение логов — `Unavailable`.
- Обязательный candidate render-probe: HTTP 200, `dry_run=true`, 20 матчей /
  40 уникальных команд / **38 секунд**, **64 663 ms**, **2 987 003 bytes**,
  максимальный RSS процесса Python **115 868 KiB**. Видео не публиковалось.
- Независимо сверены точный version ID, описание с Git/hash, ACTIVE/private,
  весь runtime/configuration hash (включая environment и pinned secret
  references), полный снимок девяти таймеров и access bindings. Всё совпало;
  production остался на прежней версии. Значения Lockbox не извлекались.

## Остановка перед promote

Штатный deploy сохранил `candidate_failed`, `error=log_read_failed`.
Повторное read-only чтение error/fatal логов этого же кандидата за интервал
с его создания до 14:56:53 МСК снова вернуло `RPC Unavailable`.
**Startup-ошибки остаются непроверенными**; поле
`startup_error_detected=false` исходного smoke не доказывает их отсутствие.

Регламент [deploy](yandex-cloud-deploy.md) считает непрочитанные логи
неуспехом smoke. Production не переключался; штатный скрипт не ослаблялся.
Разовые исключения прежних релизов к этому кандидату не применяются.
Следующий шаг — восстановить чтение журналов либо получить новое явное
разовое разрешение именно для `d4ep6970qo8ifcva2k8f`; затем переключить
тот же immutable candidate, закрепить текущую production как rollback,
проверить production dry-run, конфигурацию и все таймеры. Пересоздание
кандидата или повторная сборка не нужны.

Ручные социальные публикации не выполнялись. Проверка реального UI площадок
остаётся отдельным ограничением.

## Локальные доказательства

В этом release checkout:

- `dist/releases/d4ep6970qo8ifcva2k8f.json` — штатный failed manifest;
- `dist/schedule-panel-release/original-candidate-manifest.json` — его
  неизменённая копия с исходным logging error;
- `dist/schedule-panel-release/baseline.json` — исходный снимок и hashes;
- `dist/schedule-panel-release/candidate-verification.json` — все проверки true;
- `dist/schedule-panel-release/candidate-render-probe.json` — ответ и проверки;
- `dist/schedule-panel-release/startup-log-recheck.json` — повторный `Unavailable`.

Эта итоговая запись и указатели обновлены локально после merge; runtime
кандидата соответствует указанному merge SHA и ZIP, пересборка не выполнялась.
