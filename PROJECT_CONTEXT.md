# CS2 Results Bot — текущий контекст

## Production

- Runtime-код: `28160a3`, [PR #176](https://github.com/vovakorn/CS2Results/pull/176).
- Main: `d4eb6521dviu0ieufa5i`; VRS: `d4eu39vter6ceq500d7b`;
  monthly: `d4ep0kssaoc2t9i648ru`. Admin и Threads test не обновлялись.
- Выпуск VRS + жёлтый Beijing завершён 2 ноября в 12:33 МСК. CI:
  1 602 Python-теста на 3.11/3.12 и 31 Node; локальные/облачные render/decode,
  October source/publisher/owner-policy и main dry-run прошли.
- Владелец разрешил только этому выпуску недоступные startup-логи и сохранение
  существующего отказа regional current-source вне October monthly. Обычный
  gate остаётся непройденным; следующие релизы требуют обычной проверки.
- Rollback: прежние main `d4etf4npp6b3un02k908`, VRS `d4er62kv5q46lc29a93n`,
  monthly `d4emfnntuhrp3ie3d1gb`; профили, ACL, Gateway и 9 таймеров прежние,
  radar PAUSED. Октябрьский Reel опубликован 2 ноября в 12:38 МСК;
  подтверждены publisher `published`, journal `sent` и processed marker.
  Этот же MP4 опубликован 2 ноября в Telegram (14:05 МСК) и Threads
  (14:06 МСК), для обоих journal и processed подтверждены.
  [Ссылки и подтверждения](docs/monthly-vrs-reels.md).
  [Протокол и оставшиеся риски](docs/release-vrs-beijing-2026-11-02.md).

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

- Владелец 2 ноября разрешил общий release-цикл нового месячного шаблона:
  Node renderer + monthly publisher, нейтральный хук и короткая подпись.
  [Состав и обязательные gates](docs/release-vrs-template-2026-11-02.md).
  Production пока прежний; автодоставка Telegram/Threads вне этого diff.


- Для будущих месячных выпусков утверждён стандартный хук:
  «КАК ИЗМЕНИЛСЯ / РЕЙТИНГ CS2?». Шаблон обновлён локально,
  32 Node-теста и safe-zone/crop проверки прошли; кадр 500 ms просмотрен.
  [Правило и статус](docs/monthly-vrs-reels.md).
  Ветка `codex/monthly-vrs-hook-20261102`; production renderer ещё прежний.
  Опубликованный 2 ноября в 15:36 МСК [Instagram-пост](https://www.instagram.com/reel/DfPUcNDjhXZ/)
  с прежним хуком «КТО ВЗЛЕТЕЛ» остаётся без изменений.

- Правило владельца от 2 ноября: месячный VRS выходит в Telegram, Instagram
  и Threads с [единой короткой подписью](docs/monthly-vrs-reels.md).
  Это правило для будущих ежемесячных выпусков; текущие посты не менять.
  Исходная Telegram-подпись восстановлена, Instagram/Threads не менялись.
  Шаблон в коде подготовлен локально: 21 Python / 8 Node.
  Подключить автоматическую месячную доставку Telegram/Threads; сейчас
  production worker по-прежнему только Instagram. Новых deploy не было.

- Октябрьский VRS опубликован, media ID `18123106912905740`; проверить
  отображение/звук в реальном Instagram UI отдельно при следующем просмотре.
- Отдельно диагностировать regional current-source `502 / prepare`, одинаковый
  на старой и новой версии; он принят вне текущего месячного выпуска.

- Начальные VRS будущих Tier 1/2 LAN подготовлены независимо от анонсов;
  релиз main и одного часового timer разрешён 31 октября. Код вошёл в
  runtime `28160a3`, но отдельный timer этим выпуском не создавался.
  Прежний локальный прогон был частичным; общий CI PR #176 прошёл.
  Отдельный timer остаётся следующим шагом; исключения этого выпуска
  не разрешают его создание без профильных проверок.
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
