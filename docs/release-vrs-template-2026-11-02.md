# Выпуск будущего месячного VRS: нейтральный хук и короткая подпись

Владелец разрешил release-цикл 2 ноября 2026: «Утвреждаю заливай, если требуется».
Рабочая копия `/private/tmp/cs2-monthly-vrs-hook-20261102`, ветка
`codex/monthly-vrs-hook-20261102`, база `7bff77979bbe2b3dc052949a76d0e8f2d9f257a8`.
Собственные предыдущие правки месячного формата сохранены в этой ветке.

## Согласованный состав

- Node VRS renderer: «КАК ИЗМЕНИЛСЯ / РЕЙТИНГ CS2?» для будущих месяцев,
  Counter-Strike 2, видимый с первого кадра стартовый рейтинг, локальная карточка
  хука 0–1,2 s, постоянная компактная CS2-шапка, обложка на 500 ms.
- Node и Python monthly publisher: точная короткая подпись владельца;
  legacy receipt читается, новый контейнер получает актуальный текст.
- Сохранены 22 s / 1320 кадров, прежняя музыка, данные и движение логотипов/очков,
  старые pending/claims/processed. Текущие публикации не пересоздаются.
- Main, admin, Threads/OAuth-функции, секреты, ACL, Gateway и таймеры не меняются.
  Автодоставка месячного VRS в Telegram/Threads этим diff не реализована;
  правило трёх площадок остаётся отдельной задачей подключения.

## Проверки до выпуска

- Node: 32 passed. Measured safe-zone, square/4:5 обрезки, сентябрь/октябрь,
  три региональных формата и реальные pixel crops видимых логотипов/очков.
- Полный локальный Python-прогон: 1602 passed, единственный отказ —
  `test_available_loopback_port_is_bindable`, запрещённый bind в ограниченной
  среде. Отдельный повтор этого теста с доступом к loopback: 1 passed.
  Все 1603 теста проверены; общий CI обязан пройти без исключений.
  Python 3.12, 26 runtime-пинов и pip check проверены.
- Финальный нейтральный MP4 полностью декодирован: 1080 × 1920 / 60 fps,
  22 s / 1320 кадров, H.264 + AAC, audio peak −1,9 dB.
  SHA-256 `eb7de093d77eba843fc91174c9c811d477e873902a854739a901a83864aef536`.
  Музыка `6c5fbb29986398deacd0f3307113d9d3fa86305176aa1177790b1b7ccdc518a9`.
- Протоколы прежних реальных публикаций сохранены; их ID, SHA и заголовки
  относятся к соответствующим отправленным MP4, а не к новому default.

## Порядок выпуска и границы

После полного локального прогона: один PR → обязательный CI → merge.
Пакеты создаются из точного проверенного merge SHA в чистом release checkout.
Сначала приватные кандидаты Node и monthly с сохранением профилей и pinned
secret references; полные cloud render/decode, текущий и reviewed региональный
source, October source/publisher dry-run и startup-логи. Только затем promotion
Node → проверка совместимости monthly → monthly, production dry-run и readback
версий, rollback, конфигураций/ACL и таймеров.

Исключения прошлого выпуска не переносятся. Недоступные startup-логи или прежний
отказ regional current-source фиксируются как непройденный gate; для конкретных
новых кандидатов потребуется отдельное решение владельца. Успешный dry-run
не заменяет чтение логов. На этапе этой записи production не переключался.
Реальный тестовый пост не требуется: текущий октябрь уже доставлен, подтверждён
и защищён processed marker; проверка релиза выполняется без публикаций.

## Фактическая готовность — 3 ноября 2026

[PR #178](https://github.com/vovakorn/CS2Results/pull/178) слит в runtime SHA
`c4ae6b119cad380b9f246fc3a4dbbc6622e7aa78`. Обязательный CI прошёл:
1603 Python-теста на 3.11/3.12, 32 Node, аудит/сборки и полный Linux decode.
Основная main чистая и синхронизирована. Пакеты собраны из чистого merge checkout;
старый Xray сохранён по SHA. Профили, pinned references, ACL и таймеры проверены.

| Компонент | Новый кандидат | Пока действующий production / будущий rollback |
| --- | --- | --- |
| Node VRS | `d4eh892r0koptunqed5i` | `d4eu39vter6ceq500d7b` |
| Monthly publisher | `d4e16a3a96ii5bruumqg` | `d4ep0kssaoc2t9i648ru` |

Полные cloud render/decode Americas и monthly прошли. October source нового
Node, reviewed региональный период до 5 октября, оба dry-run monthly publisher
(включая source/owner policy) прошли. Новая короткая подпись Node подтверждена.
Main остаётся `d4eb6521dviu0ieufa5i`, Gateway и все девять таймеров прежние.
Отправок и переключения production не выполнялось.

Обычный gate не пройден: API чтения startup-логов обеих новых версий отвечает
`Unavailable`; прямой Cloud Logging также недоступен на lookup default group.
`startupLogsChecked=false`, `startupErrorDetected=null`, не «ошибок нет».
Regional current-source на старом и новом Node возвращает один и тот же
`502 / prepare`; точная причина внутри prepare не установлена. Эта проблема
существовала до нового шаблона, October monthly ей не затронут.

Владельцу предложено отдельное разовое решение для **этих двух версий**:
непроверенные startup-логи и сохранение старого regional current-source отказа
вне месячного формата. До ответа production не переключается, прежнее исключение
не переиспользуется. Новые гарантии не объявляются успешными задним числом.
[Проверяемый протокол](reviews/2026-11-03/vrs-template-readiness.json).
Raw manifests/ответы сохранены в `/private/tmp/cs2-vrs-template-release-20261102`.
