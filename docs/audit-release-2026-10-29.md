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

Выпуск завершён **29 октября в 12:45:22 МСК** со статусом
`promoted_with_log_exception`. Пользователь отдельно разрешил завершение
с разовым исключением только для четырёх точных кандидатов из таблицы ниже.
Привязки стабильных тегов и rollback проверены; все post-deploy handler/dry-run
проверки прошли. Startup-логи недоступны: `startupLogsChecked=false`,
`startupErrorDetected=null`; отсутствие startup-ошибок не подтверждено.

[PR #172](https://github.com/vovakorn/CS2Results/pull/172) слит в
`d72ad0bf8664b15c392a12c8aef847fba382cbd3`.
Итоговый [CI head `a8e3dad`](https://github.com/vovakorn/CS2Results/actions/runs/40848076407):
**1 574 passed** на Python 3.11 (257,89 s) и 3.12 (296,63 s), **27 Node passed**,
dependency audit, ZIP и Linux render/decode. Дерево merge побайтово совпало с
проверенным head; все candidate построены из чистого merge checkout.
Первый [CI состава `9fa8861`](https://github.com/vovakorn/CS2Results/actions/runs/40819154040)
также был успешен; он сохранён как история и не заменяет итоговую проверку.

Замечания PR выявили три дополнительные неточности: полнота ограниченной
выборки журнала, признак реально отправленного медиа после текстового fallback
и generation подтверждённого Reel при выключенном policy control. Они входят
в тот же PR; лимит 500 GET, существующие delivery states и очередь сохранены.
Добавлено 37 регрессий к замечаниям PR; совместный целевой прогон —
**274 passed, 1,98 s**. Все 1 574 теста прошли в итоговом CI.

Отдельное уведомление о stale generation Reel отложено: удержание корректно,
но отдельного экрана и точного notify-once для такого pending пока нет.
Прежний `_notify_admin` использует cooldown, а не гарантированное однократное
уведомление. Это открытое улучшение наблюдаемости, а не разрешение повторять
старый Reel. Для ручной проверки нужны pending и логи.

### Cloud: итог выпуска 29 октября, 12:45:22 МСК

| Функция | Действующая версия | Rollback | SHA-256 ZIP |
| --- | --- | --- | --- |
| Main | `d4etf4npp6b3un02k908` | `d4emhni9lbclf7ifgsb6` | `51c20b09626ba3727fc9f9943633a663ffeb737d7ea0eac9db0465916187ffdf` |
| Admin | `d4edre14e6im4hhslt56` | `d4e7i3o4bjdfqp529jvu` | `947f360520ed857e030b3cdfca113b412410999c63f82addd369a736bce81e48` |
| Monthly | `d4emfnntuhrp3ie3d1gb` | `d4e4lkpierd7dq7fa7ae` | `2973c24ada9b8502a0c76fe900868908bf0ffd074a399978f82592eb637fb298` |
| Threads test | `d4es9onilcqcvt5q7r9d` | `d4e9gt9ch9gugvltco43` | `2517870db790616bb13528e89b7600ec4e1cc12c62597be47cc501437008b698` |

Candidate tags: `candidate`, `candidate-admin`, `candidate`, `candidate-audit`.
Стабильные теги main/admin/monthly — `production`; ручной Threads — `threads-test`.
Тег `production` этой test-функции не добавлялся. Стабильные теги переключены
на версии таблицы; прежние версии закреплены как `rollback`.
Начало операций переноса стабильных тегов (МСК): admin 12:43:51,
monthly 12:44:09, Threads test 12:44:32, main 12:44:49.
Итоговая сверка завершена в 12:45:22 МСК (09:45:22 UTC).

- Main analytics dry-run: HTTP 200, `dry_run=true`.
- Main render-probe: 20 матчей/40 команд, 38 s, 68 434 ms, 2 716 972 bytes,
  `tournament_theme=true`; Python RSS 115 944 KiB (не общий cgroup/FFmpeg peak).
- Admin `{}`: ожидаемый handler 403 и `ok=false`.
- Monthly September 2026: оба dry-run HTTP 200; `inspect_source` подтвердил
  месяц/`dry_run=true`, readiness `publishing_enabled=true`, `owner_allowed=true`,
  generation `0`. Эти значения прочитаны, policy не изменялась.
- Threads `{}`: ожидаемый handler 404/`unknown job`. Распознанные test-jobs
  не вызывались: этот handler игнорирует `dry_run` и реально публикует их.
- Полное равенство профилей и pinned secret references с прежними immutable
  версиями проверено в памяти. Число env/references: main 30/7, admin 5/6,
  monthly 8/3, Threads 4/3. Значения не сохранены в доказательства.
- Приватность, профили/pinned references, ACL, девять таймеров (radar PAUSED)
  и Gateway сверены после promote и не изменены (`profilesUnchanged=true`,
  `infrastructureUnchanged=true`). Post-deploy smoke: main 200/dry-run,
  admin 403/`ok=false`, monthly оба 200/dry-run, Threads 404/`unknown job`.
  Тестовые внешние посты не отправлялись; реальные UI телефона/desktop не проверялись.

На этапе candidate (12:32 МСК) обычный gate остановил promote: startup-логи всех четырёх вернули
`rpc error: code = Unavailable desc = Unavailable`. Дополнительный read-only
Cloud Logging API также был недоступен. Обычный gate не пройден; отсутствие
ошибок не подтверждено: `startup_logs_checked=false`,
`startup_error_detected=null`, `release_accepted=false`.
После этого получено новое явное разрешение пользователя:
«Да, завершить этот выпуск с разовым исключением». Оно относится только к
`d4etf4npp6b3un02k908`, `d4edre14e6im4hhslt56`, `d4emfnntuhrp3ie3d1gb`
и `d4es9onilcqcvt5q7r9d`. Исходные failed manifests и ошибки сохранены;
они не переписаны как успешное прохождение обычного log gate.
В отдельном итоговом manifest — `releaseAccepted=true`,
`acceptedWithLogException=true`, `allNonLogChecksPassed=true`,
`startupLogsChecked=false`, `startupErrorDetected=null`.
Обычный smoke gate остаётся непройденным: `passed=false`,
`normalSmokeGatePassed=false`; принятие выпуска с исключением не меняет эти признаки.

[Сверка до разрешения исключения](reviews/2026-10-29/release-readiness.json),
[итог promote](reviews/2026-10-29/release-promotion.json).
Полные локальные manifests, payloads, timestamps, исходные ошибки и операторские
скрипты сохранены в ignored `dist/audit-release-20261029/`; основной оригинальный
manifest — `dist/releases/d4etf4npp6b3un02k908.json`.
Итоговый manifest принятия исключения —
`dist/audit-release-20261029/promotion-approved-exception.json`.
Отдельный receipt сохранён в приватном package bucket
`cs2results-function-packages-b1g5j8hk4gjas2vpvgqr` по ключу
`release-manifests/audit-20261029-d72ad0b-approved-exception.json`;
исходные raw failed manifests он не заменяет.

Перед candidate и promote повторно проверяются текущие версии, профиль,
private-доступ и теги таймеров. Секреты не читаются: сохраняются pinned Lockbox
references и текущие environment values без вывода их содержимого.
Main candidate создан штатным `scripts/deploy_yandex_function.sh` с семью trigger;
изолированные кандидаты — существующим manual candidate-flow. Согласованный
promote с исключением для этих четырёх версий выполнен отдельным одноразовым
операторским скриптом `dist/audit-release-20261029/promote-approved-exception.py`.
Штатный deploy-скрипт и его log gate не изменялись: недоступные логи по-прежнему
блокируют обычный promote. Это отдельное согласованное исключение не переносится
на будущие версии. Dry-run не является доказательством реальной внешней доставки.

Расхождение старой локальной папки разобрано отдельно:
[план синхронизации](local-checkout-reconciliation-2026-10-29.md).
