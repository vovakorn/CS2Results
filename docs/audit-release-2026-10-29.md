# Выпуск исправлений аудита — 29 октября 2026

Согласован пользователем сообщением «Делай» после плана единого release-цикла.
Состав: F01–F12 и R01–R05 из двух аудитов, без новых продуктовых возможностей.
База — production-код `b363ac00cb30bd242d1fa71f218c1b0d04b7eacc`,
ветка — `codex/review-fixes-20261029`. Старая рабочая папка не изменялась.

## Проверки перед PR

- Полный pytest: **1 537 passed, 268,12 s**, Python 3.12.14, точные версии всех
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

Полные локальные проверки завершены. PR, CI и cloud candidates ещё впереди.
Production ещё не переключался. Исходные production версии для rollback:

- Main — `d4emhni9lbclf7ifgsb6`;
- Admin — `d4e7i3o4bjdfqp529jvu`;
- Monthly — `d4e4lkpierd7dq7fa7ae`.

Перед candidate и promote повторно проверяются текущие версии, профиль,
private-доступ и теги таймеров. Секреты не читаются: сохраняются pinned Lockbox
references и текущие environment values без вывода их содержимого.
Для main применяется штатный deploy-скрипт с семью trigger, для изолированных
функций — их существующий manual candidate-flow.
Недоступные startup-логи блокируют promote; прежние разовые исключения
не переносятся. Dry-run не является доказательством реальной внешней доставки.
