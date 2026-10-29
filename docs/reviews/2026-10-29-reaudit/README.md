# Повторный аудит: воспроизводимые доказательства

R01–R05 уже исправлены. Эти файлы сохраняют доказательства исходного поведения,
а не зелёный набор для текущей ветки. Новые проверки ожидаемого поведения находятся
в штатных tests/test_review_reel_generations.py, tests/test_journal_read_failures.py,
tests/test_schedule_context_journal.py и tests/test_build_archives.py.

[Отчёт](../../re-audit-2026-10-29.md). Проверенная версия — `b363ac0` плюс локальные
F01–F12 в `codex/review-fixes-20261029`. Файлы ниже подтверждают наблюдаемые ошибки;
успешный proof означает воспроизведение дефекта, а не правильность реализации.

- `test_reel_owner_generation.py`: старый pending Reel публикуется после off/on.
- `test_journal_context.py`: 403/503 ошибочно дают ноль; успешный context не пишет журнал.
- `test_archive_rebuild.py`: реальный pyc из старой сборки переживает пересборку.
- `reel-owner-results.txt`, `existing-archive-cache-proof.json`: captured результаты.
- `media-summary.json`: соседние BO, streaming, Cyrillic, package probes.
- `prove_existing_archive_cache.py`: исходный одноразовый probe; для повторяемой
  проверки использовать тест `test_archive_rebuild.py` с отдельным tmp_path.

Все внешние API заменены FakeS3, botocore Stubber и транспортными заглушками.
Сборка выполняется на временной копии исходников с dummy Xray, который не запускается.
Тесты не помещены в `tests/` и не входят в штатный runtime.

```bash
CS2_REVIEW_SOURCE=/Users/user/.codex/worktrees/review-fixes-20261029/CS2results \
PYTHONPATH=/Users/user/.codex/worktrees/review-fixes-20261029/CS2results \
PYTHONDONTWRITEBYTECODE=1 \
/private/tmp/cs2results-review-py312/bin/python -m pytest -p no:cacheprovider \
  docs/reviews/2026-10-29-reaudit/test_reel_owner_generation.py \
  docs/reviews/2026-10-29-reaudit/test_journal_context.py \
  docs/reviews/2026-10-29-reaudit/test_archive_rebuild.py -q
```

После исправлений соответствующие assertions должны перестать проходить;
их следует заменить регрессиями ожидаемого поведения в штатном наборе.
