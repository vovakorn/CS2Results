# CS2 Results Bot — текущий контекст

## Production

- Runtime-код: `d72ad0b`, [PR #172](https://github.com/vovakorn/CS2Results/pull/172).
- Main: `d4etf4npp6b3un02k908`; admin: `d4edre14e6im4hhslt56`;
  monthly: `d4emfnntuhrp3ie3d1gb`; Threads test: `d4es9onilcqcvt5q7r9d`.
- Выпуск завершён 29 октября в 12:45 МСК. CI: 1 574 Python-теста на 3.11/3.12,
  27 Node; handler/dry-run/source проверки и 38 s render-probe прошли.
- Startup-логи недоступны: пользователь разрешил разовое исключение только
  для этих четырёх версий. Логи не считаются проверенными; будущие релизы
  требуют обычного gate. Rollback, профили, ACL, Gateway и 9 таймеров сохранены,
  radar PAUSED. [Протокол выпуска](docs/audit-release-2026-10-29.md).

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

- Начальные VRS будущих Tier 1/2 LAN подготовлены независимо от анонсов;
  релиз main и одного часового timer разрешён 31 октября, production ещё прежний.
  Локально 1 598 тестов прошли, один заблокирован sandbox; перед merge нужен
  зелёный CI. Startup-логи платформы недоступны, gate не пройден.
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
