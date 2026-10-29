# Доказательства ревью 28 октября 2026

Проверенный исходный код: `b363ac00cb30bd242d1fa71f218c1b0d04b7eacc`.
[Основной отчёт](../../project-review-2026-10-28.md).

Все проверки используют synthetic data и mock network/FakeS3. Они **подтверждают
существование дефектов**, поэтому успешный запуск не означает корректность кода.
Это артефакты ревью, они не включены в штатный `tests/` и runtime архив.

Файлы:

- `business-proof.py`, `business-results.json`: claim race, held queue starvation,
  cross-midnight identity и отдельная проверка неоднозначности online-требования.
- `test_delivery.py`: 13 тестов policy generation, Meta preparation и Telegram journal.
- `test_media.py`: 4 теста final maps, Cyrillic glyph selection и streaming logos.
- `test_ops.py`: 3 теста oldest-500 journal, executable bit и изолированного ZIP.
- `contradictory-final.png`, `cyrillic-standings.png`: синтетические рендеры.

Запускать на **актуальном проверенном checkout**, а не на отставшей основной папке.
Путь `CS2_REVIEW_SOURCE` должен указывать на этот checkout. Для ZIP-теста сначала
собрать `scripts/build_social_publish_zip.sh` в том же checkout; сборка может
скачивать закреплённый Xray binary, если его нет в build cache.

Пример из основной папки, если изолированный snapshot аудита ещё существует:

```bash
CS2_REVIEW_SOURCE=/private/tmp/cs2results-audit-20261028 \
PYTHONPATH=/private/tmp/cs2results-audit-20261028 \
/Users/user/Projects/CS2results/.venv/bin/python -m pytest \
  /Users/user/Projects/CS2results/docs/reviews/2026-10-28/test_delivery.py \
  /Users/user/Projects/CS2results/docs/reviews/2026-10-28/test_media.py \
  /Users/user/Projects/CS2results/docs/reviews/2026-10-28/test_ops.py -q

/Users/user/Projects/CS2results/.venv/bin/python \
  /Users/user/Projects/CS2results/docs/reviews/2026-10-28/business-proof.py \
  /private/tmp/cs2results-audit-20261028
```

На дату ревью: 20 passed; business probe exit 0. После исправления дефекта
соответствующий proof ожидаемо перестанет проходить. Не ослаблять исправление
ради сохранения этих исходных assertions.
