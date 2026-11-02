# Общий выпуск месячного VRS и темы Beijing — 2 ноября 2026

Пользователь разрешил публикацию согласованной версии месячного VRS и включение
исправления цвета ESL/IEM Beijing. База: `d50932f7d544fa65db947812f4dba228491439d5`,
рабочая копия `/private/tmp/cs2-monthly-vrs-diagnosis`, ветка
`codex/diagnose-october-monthly-vrs`.

## Состав

- Node VRS renderer: текущий мировой топ-20 на каждой дате, вход/выход снизу,
  сохранение всех сплошных следов, компактная шапка без дат, логотипы 34 px,
  движущиеся целые очки; 22 s / 1320 кадров. Указанный владельцем трек CS2.
- Monthly publisher: проверка нового контракта 1320 кадров.
- Main: варианты `IEM — Beijing season 2026` и полное имя с `season 2026`
  распознаются как проверенная жёлтая тема `iem-beijing`, `#FFFF2E`.
  Другие города IEM и идентификаторы турниров не меняются.
- Linux render-probe различает 22 s monthly и прежние 32,4 s региональных Reels.

Другие незавершённые worktree, новый baseline timer, admin/social/Threads-функции
не включены в выпуск. Существующий код baseline уже находится в origin/main;
его дополнительное включение этим выпуском не выполняется.

## Проверки

- Python 3.12.14, все пины requirements.txt проверены; pip check успешен.
- Полный pytest: **1602 passed**, 279,71 s; Node: **31 passed**.
- Beijing: offline карточки на 1, 4 и 10 матчей, длинное кириллическое имя,
  fallback логотипов; 1080 × 1350, жёлтая тема подтверждена.
- Согласованный October MP4: полный encode/decode, 1080 × 1920, 60 fps,
  1320 кадров, 22 s, AAC, peak −1,9 dB.
- Main и monthly ZIP собраны с прежним Xray v26.6.27; Node ZIP содержит
  новую музыку и её SHA-256 manifest. Точные пакеты использованы при выпуске.

## Проверенное исходное состояние production

- Main `d4etf4npp6b3un02k908` (`d4e6e13rlrl7go01m2q2`).
- VRS renderer `d4er62kv5q46lc29a93n` (`d4e8n07roso4762pf1h4`).
- Monthly publisher `d4emfnntuhrp3ie3d1gb` (`d4es8uk4gjgioi5n7lek`).
- Девять таймеров, все привязки по `production`; radar PAUSED. Конфигурация
  и pinned secret references сохраняются. Секретные payload не читаются.

## Снимок подготовки до разрешения исключений

[PR #176](https://github.com/vovakorn/CS2Results/pull/176) слит в
`28160a3619db17a220f359d57bdc6d70b2c374aa`. CI успешен на Python 3.11/3.12,
аудит зависимостей и Linux encode/decode обоих форматов успешны. Основная main
чистая и совпадает с origin/main. Пакеты кандидатов собраны из точного merge SHA
в чистом release checkout. Состояние ниже зафиксировано до разрешения исключений;
фактический итог выпуска приведён в следующем разделе.

| Назначение | Кандидат | Текущий production / rollback при выпуске |
| --- | --- | --- |
| Main / Beijing | `d4eb6521dviu0ieufa5i` | `d4etf4npp6b3un02k908` |
| Monthly publisher | `d4ep0kssaoc2t9i648ru` | `d4emfnntuhrp3ie3d1gb` |
| VRS renderer | `d4eu39vter6ceq500d7b` | `d4er62kv5q46lc29a93n` |

Main dry-run — 200; максимальный schedule render-probe — 38 s / 86 428 ms,
115 608 KiB RSS, все проверки успешны. Monthly dry-run и source/owner-policy
probe сентября — 200, publishing enabled / owner allowed. Он пока вызывает
старый production renderer; октябрьский inspect_source проверяется перед
переключением publisher, после переключения Node.

Новый Node-кандидат принял реальный октябрь: пять дат, 20 команд на дате,
26 уникальных команд. Полный облачный месячный render/decode: 22 s / 1320
кадров, 1080 × 1920, AAC, peak −1,9 dB, 64,71 s рендера. Americas probe
штатного candidate-скрипта прошёл до следующего отказавшего source check.
Старый проверенный региональный период до 5 октября также прошёл на кандидате.

### Непройденные gates

1. Startup-логи всех трёх точных кандидатов недоступны (`Unavailable`), в том
   числе при попытке прямого доступа к Cloud Logging. Это не доказательство
   отсутствия ошибок. В исходном main manifest `startup_error_detected=false`
   является значением по умолчанию после отказа чтения; в сводке оно `null`.
2. Текущий региональный `job=capture` возвращает `502 / stage=prepare` как
   на прежней production, так и на кандидате. Точная причина внутри prepare
   не установлена. Это существующая проблема длинной истории между Major,
   не отказ октябрьского monthly: его реальный source dry-run прошёл.
   Штатный VRS promote повторяет текущий capture и без отдельного решения
   откатит переключение. Проверка не удалялась и не переписывалась как успешная.

**На этапе подготовки production ещё не был переключён, публикаций не было.**
Для продолжения нужно отдельное решение владельца: разовое исключение по
недоступным логам именно для трёх кандидатов и принятие существующего отказа
регионального current-source gate вне месячного формата либо его отдельное
исследование/исправление. Прежние исключения не переиспользуются.

[Проверяемая сводка](reviews/2026-11-02/release-readiness.json).
Оригинальные raw manifests и ответы не изменены, находятся в
`/private/tmp/cs2-release-20261102` и ignored dist release checkout.
После решения проверяются точные теги, конфигурации и таймеры; порядок
переключения renderer → October source check monthly candidate → monthly → main.
Месячный пост остаётся в обычном окне 1–3 ноября, 12:00–15:59 МСК с дедупликацией.

## Выпуск завершён с разрешёнными исключениями — 2 ноября, 12:33 МСК

Владелец явно разрешил оба исключения для трёх кандидатов из таблицы.
Они переключены на production из runtime-коммита
`28160a3619db17a220f359d57bdc6d70b2c374aa`; прежние production закреплены
тегами rollback. Профили, pinned secret references, ACL, девять таймеров и
Gateway проверены до/после и сохранены.

Порядок: Node → October source check месячного кандидата → monthly → main.
Все scoped non-log smokes успешны: реальный октябрьский source нового Node,
ранее проверенный региональный период до 5 октября, October inspect_source
месячного publisher (publishing enabled / owner allowed), main analytics dry-run.
Отправок smoke-проверками не выполнялось.

Обычный gate не объявляется пройденным: `normalReleaseGatePassed=false`,
`startupLogsChecked=false`, `startupErrorDetected=null`. `releaseAccepted=true`
и `acceptedWithExceptions=true` означают отдельное решение владельца, а не
успешное чтение журналов или исправление регионального current-source.
Следующий выпуск требует обычных проверок; исключения не переносятся.

Октябрьский мировой Reel **опубликован 2 ноября в 12:38:24 МСК**.
Publisher сначала вернул `processing`, затем `published` для того же pending
container. Подтверждены общий journal `status=sent` (не тест) и processed marker;
Instagram media ID: `18123106912905740`, UID:
`instagram_vrs_monthly_world_2026-10`.
Фактически отправленный MP4: 22 s / 1320 кадров / 1080 × 1920 / 60 fps,
fullDecode passed, SHA-256
`93a89b8255124663d988d40efc5abe122660ae2ffa2b74e7a52bbc8e1961c670`.
Музыкальный SHA-256 совпадает с согласованным 22 s фрагментом.

Дополнительный контрольный non-dry вызов для проверки `already_sent` был
отклонён автоматической проверкой разрешений как риск повторной отправки.
Он не выполнен и не обходился; итог подтверждён read-only чтением journal и
processed marker. Новая публикация этим контрольным действием не создавалась.
Отдельный просмотр Instagram UI на телефоне не выполнялся; факт публикации
подтверждён ответом API и внутренним журналом доставки.

[Фактический протокол переключения](reviews/2026-11-02/release-promotion.json).
Документационная ветка `codex/vrs-beijing-release-readiness-20261102` новее
runtime; изменение документации само по себе не требует повторного deploy.
