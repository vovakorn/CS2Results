# Выпуск исправлений аудита — 29 октября 2026

Согласован пользователем сообщением «Делай» после плана единого release-цикла.
Состав: F01–F12 и R01–R05 из двух аудитов, без новых продуктовых возможностей.
База — production-код `b363ac00cb30bd242d1fa71f218c1b0d04b7eacc`,
ветка — `codex/review-fixes-20261029`. Старая рабочая папка не изменялась.

## Проверки перед PR

- Полный pytest исходного состава `9fa8861`: **1 537 passed, 268,12 s**, Python 3.12.14, точные версии всех
  26 runtime-зависимостей. Включён loopback-тест; отказов и пропусков нет.
- Ревью diff основного pipeline, бизнес-логики и пакетов: блокеров не найдено.
- Compileall, pip check, bash -n, diff check; сборки main/admin/social/OAuth.
- 42 offline PNG: плотность 1/4/10, длинные имена, логотипы и кириллица.
  [Manifest и выбранные превью](reviews/2026-10-29/review-manifest.json).
- Упаковка monthly publisher с прежним pinned Xray также выполнена.

## Состав cloud release

| Функция | Причина | Обязательная проверка |
| --- | --- | --- |
| Main `d4e6e13rlrl7go01m2q2` | Доставка, очередь, модели и renderer | Analytics dry-run, startup-логи, Reel render-probe |
| Admin `d4erm6ek4ph9qegng4gb` | Журнал и его счётчики | Пустое событие → 403, startup-логи |
| Monthly Python `d4es8uk4gjgioi5n7lek` | Общие storage claims и Instagram helper | Monthly dry-run, inspect_source, startup-логи |
| Threads test `d4e7l0puleo82h5p61gj` | Social ZIP, renderer и Threads helper | Пустое событие → 404, startup-логи; renderer проверен offline |

OAuth и Node VRS runtime не менялись, их повторный deploy не нужен.
Новые сервисы, миграции, таймеры, повторные публикации и backfill не входят в выпуск.

## Текущий этап

[PR #172](https://github.com/vovakorn/CS2Results/pull/172) объединяет исправления.
Первый [CI состава `9fa8861`](https://github.com/vovakorn/CS2Results/actions/runs/40819154040)
успешен: Python 3.11/3.12, Node, dependency audit и Linux video probes.
Итоговый CI обязан соответствовать последнему head PR; результаты предыдущего
head не заменяют эту проверку. Cloud candidate и promote проверяются отдельно.

Замечания PR выявили три дополнительные неточности: полнота ограниченной
выборки журнала, признак реально отправленного медиа после текстового fallback
и generation подтверждённого Reel при выключенном policy control. Они входят
в тот же PR; лимит 500 GET, существующие delivery states и очередь сохранены.
Добавлено 37 регрессий к замечаниям PR; совместный целевой прогон —
**274 passed, 1,98 s**. Полный итоговый набор содержит 1 574 теста и проверяется
в CI нового head, а не результатом прежнего состава.

Отдельное уведомление о stale generation Reel отложено: удержание корректно,
но отдельного экрана и точного notify-once для такого pending пока нет.
Прежний `_notify_admin` использует cooldown, а не гарантированное однократное
уведомление. Это открытое улучшение наблюдаемости, а не разрешение повторять
старый Reel. Для ручной проверки нужны pending и логи.

На этапе подготовки PR production ещё не переключался. Исходные версии:

- Main — `d4emhni9lbclf7ifgsb6`;
- Admin — `d4e7i3o4bjdfqp529jvu`;
- Monthly — `d4e4lkpierd7dq7fa7ae`.
- Threads test — `d4e9gt9ch9gugvltco43`, стабильный тег `threads-test`;
  тега `production` у этой ручной функции нет, он не добавляется.

Перед candidate и promote повторно проверяются текущие версии, профиль,
private-доступ и теги таймеров. Секреты не читаются: сохраняются pinned Lockbox
references и текущие environment values без вывода их содержимого.
Для main применяется штатный deploy-скрипт с семью trigger, для изолированных
функций — их существующий manual candidate-flow.
Недоступные startup-логи блокируют promote; прежние разовые исключения
не переносятся. Dry-run не является доказательством реальной внешней доставки.
