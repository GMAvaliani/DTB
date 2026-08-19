# DTB — Development

## Установка и запуск

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
dtb serve --reload          # http://localhost:8000 (UI + /docs)
dtb demo                    # демо-модель в БД
```

Данные — SQLite в `./data/dtb.db` (переопределяется `DTB_DATABASE_URL` /
`DTB_DATA_DIR`). Фронтенд — статические файлы `frontend/` без сборки.

## Структура

```
src/dtb/
  common/       safe-выражения, детерминированные seed'ы, id
  domain/       pydantic-модель двойника (см. DOMAIN_MODEL.md)
  simulation/   движок (см. SIMULATION_ENGINE.md) — не импортирует ничего выше
  graph/        граф знаний (см. KNOWLEDGE_GRAPH.md)
  analysis/     Монте-Карло, прогноз, bottlenecks, сравнение
  optimization/ objective/variables/optimizers (см. OPTIMIZATION.md)
  validation/   валидация модели + data quality
  integration/  импорт/коннекторы (см. ERP_INTEGRATION.md)
  persistence/  SQLAlchemy, версии, аудит, серии
  services/     фоновые job'ы (точка выделения воркеров)
  api/          FastAPI: маршруты, безопасность
  ai/           ассистент (см. AI_ARCHITECTURE.md)
  demo/         демо-модель «Демо-Завод»
  cli.py        команды serve/demo/simulate/validate/export-template
frontend/       SPA (vanilla JS, SVG)
tests/          unit / integration / e2e
docs/           документация + ADR
```

Правила слоёв: `simulation` зависит только от `domain`+`common`; `domain` ни от
чего; доменная логика никогда не живёт во фронтенде; результаты симуляций всегда
несут provenance.

## Тесты

```bash
python -m pytest                  # всё (~6 с)
python -m pytest tests/unit       # движок, домен, граф, анализ, оптимизация
python -m pytest tests/integration  # БД, импорт/экспорт, API, auth
python -m pytest tests/e2e        # полная приёмочная цепочка (Section 44)
```

Обязательные инварианты, закреплённые тестами:

- детерминизм: одинаковые (model, scenario, initial state, seed) ⇒ одинаковый
  `content_digest()` (в т.ч. для демо-модели и Монте-Карло);
- семантика СЭД: потребление на старте / производство на завершении, ожидание
  входов, конкуренция за ресурсы, приостановка при потере ёмкости с дорасходом
  остатка длительности, вероятностные ветви (частоты сходятся);
- Excel-экспорт ↔ импорт эквивалентен (включая демо-модель);
- симуляция запрещена при критических ошибках валидации;
- роли/скоупы API (viewer/editor/admin, tenant).

## Как добавлять

- **Новый эффект события**: `EffectKind` + ветка в `engine._apply_effect` +
  валидация + тест.
- **Новый оптимизатор**: класс с `optimize(...)` → `register_optimizer`.
- **Новый ERP-адаптер**: класс с `system` и `fetch()` → при необходимости новые
  `object_type` в `integration.base._normalize`.
- **Новый вид длительности/потока**: enum + сэмплер + валидатор + тест.

Перед PR: `python -m pytest`; для UI — открыть основные экраны на демо-модели
(JS без сборки, ошибки видны в консоли браузера).
