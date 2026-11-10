# CS2 Results Bot — текущий контекст

## Production

- Main runtime `28160a3`, production `d4eb6521dviu0ieufa5i` (Beijing и бот);
  в выпуске 3 ноября main, admin и Threads-функции не обновлялись.
- VRS renderer / monthly publisher: runtime `c4ae6b1`,
  [PR #178](https://github.com/vovakorn/CS2Results/pull/178).
  Production Node `d4eh892r0koptunqed5i`, monthly `d4e16a3a96ii5bruumqg`.
- Выпуск 3 ноября в 09:55 МСК: будущие месячные Reels используют
  «КАК ИЗМЕНИЛСЯ / РЕЙТИНГ CS2?», Counter-Strike 2 и короткую подпись.
  CI: 1603 Python на 3.11/3.12, 32 Node, Linux build/render/decode прошли;
  production October source/owner policy и reviewed regional dry-run успешны.
- Владелец разрешил обоим точным кандидатам недоступные startup-логи и
  сохранение прежнего regional current-source `502 / prepare` вне monthly.
  Обычный gate остаётся false; исключения не действуют для следующих выпусков.
- Rollback Node `d4eu39vter6ceq500d7b`, monthly `d4ep0kssaoc2t9i648ru`.
  Профили, pinned references, ACL, Gateway и 9 таймеров сохранены; radar PAUSED.
  В этом выпуске постов не отправлялось. Автодоставка monthly пока только
  Instagram; Telegram/Threads остаются отдельной задачей.
  [Фактический протокол и ограничения](docs/release-vrs-template-2026-11-02.md).
- Октябрь уже опубликован в Instagram/Telegram/Threads; отдельный Instagram
  вариант с прежним хуком вышел 2 ноября в 15:36 МСК и сохранён.
  [Ссылки на публикации](docs/monthly-vrs-reels.md).

## Рабочие копии

- Основная папка: `/Users/user/Projects/CS2results`, чистый `main`.
  Восстановлена fast-forward из старой базы `0cddf48` после полного backup.
- Старые 35 tracked-правок, 1 342 untracked-записи, ignored-материалы и общий
  Git-каталог сохранены локально вне Git; связи 13 worktree не изменены.
  [Разбор и проверка сохранности](docs/local-checkout-reconciliation-2026-10-29.md).
- Работа по задачам — в отдельных ветках/worktree от свежего `origin/main`.
  После merge ответственный синхронизирует чистую основную main и проверяет
  `HEAD == origin/main`. [Рабочий процесс](docs/local-workflow.md).
- Одобренная аватарка профилей — `assets/channel-avatar.png`, кубок CS2 640 × 640.
  Runtime-логотип карточек `cs2bot/assets/channel-logo.png` не менялся.

## Следующие задачи и источники

- PR #180 слит (`7f90830`), CI зелёный; локально 1 653 теста прошли.
  Первый кандидат `d4e9asgmu4rersa08ldh` не переключён: startup-логи Unavailable,
  точечный LP-запрос отклонён из-за отсутствующего AND. Live контроль с AND дал
  200; follow-up исправляет запрос и диагностику. Production прежний, постов нет.
  Основная локальная main защищена от записи в общий `.git`; актуальный клон:
  `dist/tournament-publication-lifecycle-2026-11-10/checkout`.

- 10 ноября: реализован durable реестр трёх обязательных публикаций турнира
  с независимой подготовкой, повторами, отдельным UID финала и before capture.
  Выпуск и восстановление Beijing разрешены владельцем; полный локальный
  release-набор пройден: 1 649 тестов, сборка с Xray и визуальная проверка. Владелец прямо подтвердил
  push/PR/CI/merge/deploy и восстановление постов; ветка отправлена в GitHub,
  идёт CI/release. Перед слиянием дополнительно ограничен охват публикаций:
  широкий календарь Tier 1/2 сохраняет before, а публичные задачи требуют
  действующего редакционного допуска и повторной проверки исключений. Production и посты пока не изменены.
  [Механизм, миграция и выпуск](docs/tournament-publications.md).

- 9 ноября: локально исправлен recovery финала Beijing — точные PandaScore ID,
  join разных написаний и scoped LP parent вместо зависимости от latest-N.
  Production main сверена: `d4eb6521dviu0ieufa5i`; timeout state подтверждён,
  before Beijing существует, after в event-папке отсутствует. Код не выложен,
  постов не было; [факты, границы и восстановление](docs/beijing-final-incident-2026-11-09.md).

- Выпуск нейтрального месячного шаблона завершён 3 ноября;
  [версии, проверки и согласованные исключения](docs/release-vrs-template-2026-11-02.md).

- Для будущих месячных выпусков утверждён стандартный хук:
  «КАК ИЗМЕНИЛСЯ / РЕЙТИНГ CS2?». Шаблон действует в production,
  32 Node-теста и safe-zone/crop проверки прошли; кадр 500 ms просмотрен.
  [Правило и статус](docs/monthly-vrs-reels.md).
  Runtime `c4ae6b1`; отдельная документационная ветка новее runtime.
  Опубликованный 2 ноября в 15:36 МСК [Instagram-пост](https://www.instagram.com/reel/DfPUcNDjhXZ/)
  с прежним хуком «КТО ВЗЛЕТЕЛ» остаётся без изменений.

- Правило владельца от 2 ноября: месячный VRS выходит в Telegram, Instagram
  и Threads с [единой короткой подписью](docs/monthly-vrs-reels.md).
  Это правило для будущих ежемесячных выпусков; текущие посты не менять.
  Исходная Telegram-подпись восстановлена, Instagram/Threads не менялись.
  Короткая подпись и обложка выпущены 3 ноября: CI 1603 Python / 32 Node.
  Подключить автоматическую месячную доставку Telegram/Threads; сейчас
  production worker по-прежнему только Instagram.

- Октябрьский VRS опубликован, media ID `18123106912905740`; проверить
  отображение/звук в реальном Instagram UI отдельно при следующем просмотре.
- Отдельно диагностировать regional current-source `502 / prepare`, одинаковый
  на старой и новой версии; он принят вне текущего месячного выпуска.

- Начальные VRS будущих Tier 1/2 LAN подготовлены независимо от анонсов;
  релиз main и одного часового timer разрешён 31 октября. Код вошёл в
  runtime `28160a3`, но отдельный timer этим выпуском не создавался.
  Прежний локальный прогон был частичным; общий CI PR #176 прошёл.
  В новом выпуске эту работу выполняет единый 15-минутный
  `tournament_publications`; отдельный hourly timer больше не планируется.
  [Результат и включение](docs/vrs-baselines.md).
- Проверить первые штатные публикации и реальные UI после релиза.
- Выбрать отображение удержанного по старому generation Reel в админке;
  старые pending не возобновлять автоматически.
- [Очередь](BACKLOG.md), [следующий план](docs/next-plan-2026-10-29.md),
  [подробное production-состояние](PROJECT_STATUS.md).
- Продуктовые правила — `docs/requirements.md`; контракты — `docs/data-contract.md`;
  устройство системы — `docs/architecture.md`; запуск — `README.md`.
- Полная прежняя история сохранена в [PROJECT_HISTORY.md](PROJECT_HISTORY.md).
  Не загружайте её при обычном старте задачи.
