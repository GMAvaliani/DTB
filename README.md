# DTB — Digital Twin of Business

Платформа для создания и эксплуатации **цифрового двойника деятельности
предприятия** (бизнеса, подразделения, системы систем).

Предприятие моделируется как динамическая система: его состояние — вектор
параметров **полезностей**, деятельность — иерархия **СЭД**ов (структурных
элементов деятельности), действующих как агенты; внешняя среда воздействует
потоками **НП-событий**; симуляция идёт в непрерывном модельном времени между
**моментами переключения** (МП). Поверх исполняемой модели работают Монте-Карло,
прогноз достижения целей, what-if, сравнение сценариев, анализ узких мест,
оптимизация управленческих решений и граф знаний.

Предметная спецификация: `Алгоритм_2025_10_16.docx`, `Платформа_2026_06.docx`
(source of truth доменной модели).

## Быстрый старт

Требуется Python 3.11+.

```bash
pip install -e ".[dev]"
dtb serve                 # API + веб-интерфейс на http://localhost:8000
```

Дальше в интерфейсе: **Предприятия → «Создать демо-модель „Демо-Завод“»** — и
можно сразу пройти весь цикл: цели → СЭДы → сценарии → симуляция → прогноз →
сравнение → оптимизация → граф знаний.

То же из терминала:

```bash
dtb demo                                  # создать демо-предприятие в БД
dtb simulate demo_baseline --seed 42      # один прогон, JSON-результат
dtb simulate demo_baseline --runs 20      # Монте-Карло
dtb validate demo_plant                   # валидация модели + data quality
dtb export-template model.xlsx            # пустой Excel-шаблон модели
```

Или в Docker:

```bash
docker build -t dtb . && docker run -p 8000:8000 dtb
```

## Что внутри

| Слой | Пакет | Кратко |
|---|---|---|
| Доменная модель | `dtb.domain` | Enterprise, Goal, СЭД, Utility, Resource, Organization, Equipment, ERP-объекты, НП-события, KPI, Scenario |
| Движок симуляции | `dtb.simulation` | event-driven continuous-time, пулы ожидающих/выполняемых СЭД, пуассоновские/плановые/внешние потоки, Mode A/Mode B, детерминизм по seed |
| Граф знаний | `dtb.graph` | проекция модели, HAS_GOAL/PRODUCES/CONSUMES/TRIGGERS/…, impact/dependency-анализ |
| Аналитика | `dtb.analysis` | Монте-Карло, прогноз (факт/план/прогноз + CI), bottlenecks, сравнение сценариев |
| Оптимизация | `dtb.optimization` | objective/variables/constraints, grid & random search, plugin-реестр |
| Валидация | `dtb.validation` | ворота перед симуляцией + Data Quality Report |
| Интеграция | `dtb.integration` | Excel/CSV/JSON-импорт (структура книги по видам 1–8), mock-ERP, generic REST-коннектор |
| Хранение | `dtb.persistence` | SQLite→PostgreSQL, версии модели с diff, audit trail, time-series |
| API | `dtb.api` | REST + SSE-прогресс, роли viewer/editor/admin, tenant-изоляция |
| UI | `frontend/` | SPA без сборки: 19 экранов рабочего процесса двойника |
| AI | `dtb.ai` | rule-based ассистент поверх модели + интерфейс LLM-провайдера |

## Документация

- **[docs/USER_GUIDE.md](docs/USER_GUIDE.md) — руководство пользователя: установка, принципы работы, сквозной пример построения модели, справочники**
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — архитектура и bounded contexts
- [docs/DOMAIN_MODEL.md](docs/DOMAIN_MODEL.md) — доменная модель и 8 видов описания
- [docs/SIMULATION_ENGINE.md](docs/SIMULATION_ENGINE.md) — движок: цикл МП, семантика СЭД
- [docs/KNOWLEDGE_GRAPH.md](docs/KNOWLEDGE_GRAPH.md) — граф знаний и causal-запросы
- [docs/API.md](docs/API.md) — REST API (также: `/docs` на запущенном сервере)
- [docs/ERP_INTEGRATION.md](docs/ERP_INTEGRATION.md) — коннекторы и импорт
- [docs/OPTIMIZATION.md](docs/OPTIMIZATION.md) — оптимизационный контур
- [docs/AI_ARCHITECTURE.md](docs/AI_ARCHITECTURE.md) — AI-слой
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — разработка и тесты
- [docs/ROADMAP.md](docs/ROADMAP.md) — что сделано и что дальше
- [docs/adr/](docs/adr/) — Architecture Decision Records

## Тесты

```bash
python -m pytest            # unit + integration + e2e (95 тестов)
```

Ключевой инвариант: одинаковые (модель, сценарий, начальное состояние, seed) ⇒
бит-идентичный результат симуляции (`SimulationResult.content_digest()`).

## Конфигурация

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `DTB_DATABASE_URL` | SQLAlchemy URL | `sqlite:///./data/dtb.db` |
| `DTB_DATA_DIR` | каталог данных для SQLite | `./data` |
| `DTB_API_KEYS` | `key:role[:enterprise_id],…` (roles: viewer/editor/admin) | не задано → открытый dev-режим |
| `DTB_EXTERNAL_MODEL_HOSTS` | allowlist хостов внешних моделей (Mode B) | не задано → любой http(s) |
| `DTB_FRONTEND_DIR` | путь к статике UI | автоопределение |
