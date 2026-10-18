# DTB — REST API

Базовый префикс `/api`. Интерактивная спецификация — `/docs` (OpenAPI) на
запущенном сервере. Аутентификация: заголовок `X-API-Key` (см. README,
`DTB_API_KEYS`); роли viewer < editor < admin; ключ может быть ограничен одним
предприятием (tenant-изоляция). Без настройки ключей — открытый dev-режим.

## Предприятия и модель

```
GET    /enterprises                        список
POST   /enterprises                        создать               (editor)
GET    /enterprises/{eid}                  сводка + счётчики
DELETE /enterprises/{eid}                  удалить               (admin)
GET    /enterprises/{eid}/model            полный снимок модели
PUT    /enterprises/{eid}/model            заменить (с валидацией)
GET    /enterprises/{eid}/entities/{etype}             коллекция
POST   /enterprises/{eid}/entities/{etype}             создать/обновить
GET    /enterprises/{eid}/entities/{etype}/{id}
DELETE /enterprises/{eid}/entities/{etype}/{id}
PUT    /enterprises/{eid}/initial-state
GET    /enterprises/{eid}/series/{plans|actuals}
PUT    /enterprises/{eid}/series/{plans|actuals}/{kpi_id}
```

`{etype}`: goal | utility_type | sed | resource | org_unit | employee |
equipment | erp_object | event_kind | kpi | subject | control_action | policy |
decision_point | observation (+ заготовки link | gap_type — движком не
используются).

## Версии, валидация, качество данных

```
POST /enterprises/{eid}/versions           снимок версии (+bump)
GET  /enterprises/{eid}/versions
GET  /enterprises/{eid}/versions/{v}
GET  /enterprises/{eid}/versions-diff?from=1&to=2   (to=0 — текущая рабочая)
GET  /enterprises/{eid}/validation         ворота симуляции
GET  /enterprises/{eid}/data-quality       Data Quality Report
```

## Граф знаний и AI

```
GET  /enterprises/{eid}/graph[?types=sed,goal]
GET  /enterprises/{eid}/graph/neighborhood/{node}?depth=2
GET  /enterprises/{eid}/graph/impact/{node}
GET  /enterprises/{eid}/graph/dependencies/{node}
GET  /enterprises/{eid}/graph/paths?source=&target=
POST /enterprises/{eid}/graph/entities     пользовательский узел
POST /enterprises/{eid}/graph/relations    пользовательская связь
GET  /enterprises/{eid}/ai/explain/{node}
GET  /enterprises/{eid}/ai/search?q=
GET  /enterprises/{eid}/ai/suggestions     потенциально отсутствующие связи
GET  /enterprises/{eid}/ai/questions       вопросы к модели
```

## Импорт / экспорт / ERP

```
POST /enterprises/{eid}/import/csv?sheet=goals   (multipart file)
POST /enterprises/{eid}/import/excel
POST /enterprises/{eid}/import/json
POST /enterprises/{eid}/erp/sync-mock            агент-Актуализатор (mock ERP)
GET  /enterprises/{eid}/export/excel|json|graph-json
GET  /import/template                            пустая Excel-книга
POST /demo[?which=plant|farm|all]                демо-предприятия + сценарии
POST /enterprises/{eid}/import/estimate?project_id=&project_name=&sheet=
                                                 смета со статусом → проект и пакеты работ
```

## Сценарии

```
GET/POST /scenarios[?enterprise_id=]
GET/PUT/DELETE /scenarios/{id}
POST /scenarios/{id}/whatif        производный what-if сценарий
```

## Симуляции

```
POST /simulations                  {scenario_id, kind: single|monte_carlo,
                                    runs?, seed?, label?} → 202 + id (фон. job)
GET  /simulations[?enterprise_id=&scenario_id=]
GET  /simulations/{id}             статус + provenance + прогресс
POST /simulations/{id}/pause | /resume | /cancel
GET  /simulations/{id}/state       прогресс (если идёт) или финальное состояние
GET  /simulations/{id}/events      журнал НП-событий/переходов СЭД
GET  /simulations/{id}/timeline    KPI/сток/пул-траектории + статистика
GET  /simulations/{id}/results     полный результат (single или Монте-Карло)
GET  /simulations/{id}/bottlenecks
GET  /simulations/{id}/decisions            журнал МР: триггеры + интервал
GET  /simulations/{id}/policy-trace[?policy_id=]  вызовы политик по контурам
GET  /simulations/{id}/observation-error    |Y − Ŷ| по компонентам состояния
GET  /simulations/{id}/lookahead            просмотр вперёд: кандидаты и оценки
GET  /simulations/{id}/rejected-actions     отклонённые и неудавшиеся действия
                                            + стоимость управления по контурам
GET  /simulations/{id}/economics            NPV, статьи, разогрев
GET  /simulations/{id}/verification         отчёт верификации прогона:
                                            баланс полезностей, закон Литтла,
                                            учёт ресурсов, сходимость экономики
GET  /simulations/{id}/stream      SSE-прогресс (text/event-stream)
GET  /simulations/compare?ids=a,b  сравнение (первый — baseline): разности
                                   средних, парные разности по сопряжённым
                                   прогонам с 95%-ДИ, экономика по статьям
```

Запуск блокируется с 422, если валидация модели или сценария имеет критические
ошибки.

## Прогноз и оптимизация

```
Пути переменных решения: `resource:<id>`, `event_rate:<id>`,
`sed_duration:<id>`, `stock:<id>`, `param:<тип>:<id>.<поле>`,
`policy:<policy_id>.<порог>` (порог политики контура управления).

```
GET  /forecasts?scenario_id=[&simulation_id=]   Вид 3: факт/план/прогноз+CI,
                                                P(цели), bottlenecks, ресурсы
GET  /forecasts/deviations?scenario_id=         причины отклонений
POST /optimizations                {scenario_id, variables[], objective_kpi_id
                                    | objective_expression, direction,
                                    constraints[], runs_per_evaluation, seed,
                                    optimizer: grid_search | random_search |
                                    coordinate_search, quantile, max_evaluations}
                                    → 202
GET  /optimizations/{id}
```

## Верификация

```
GET /verification/benchmarks[?names=mm1,mmc,dd1,discounting,little
                             &seed=&horizon=&replications=]
```

Сверка движка с аналитическими эталонами: M/M/1, M/M/c (Эрланг C), D/D/1,
непрерывное дисконтирование, закон Литтла. Эталонные модели собираются
модулем верификации, модель предприятия не нужна.

## Проекты (EVM), финансы, мониторинг — версия 0.2

```
GET  /enterprises/{eid}/projects                         проекты модели: пакеты, BAC, базовый план
GET  /enterprises/{eid}/projects/{pid}/evm-status        EVM на дату статуса по фактам (без прогона)
POST /enterprises/{eid}/projects/baseline[?project_id=]  базовый план детерминированным прогоном

GET  /simulations/{id}/reports/evm                       PV/EV/AC, индексы, распределения по прогонам
GET  /simulations/{id}/reports/evm/{pid}/packages        пакеты работ на конец прогона
GET  /simulations/{id}/reports/cash-flow?period=&until=  ОДДС: hour|day|week|month|quarter|year|total|<часы>
GET  /simulations/{id}/reports/income?period=&until=     ОПиУ по начислениям (капвложения отдельно)
GET  /simulations/{id}/reports/cash-balance              остаток денег на конец каждых суток
GET  /simulations/{id}/reports/cash-gap[?threshold=]     кассовые разрывы + вероятность по прогонам
GET  /simulations/{id}/reports/payment-calendar?from_hours=&days=
GET  /simulations/{id}/reports/variance?plan=<sim_id>    план-факт по статьям и периодам
GET  /simulations/{id}/reports/dashboard?at=&window_days= онлайн-панель на любой момент
GET  /simulations/{id}/alerts?period=&delay_days=        сигналы мониторинга и запаздывание отчётности
GET  /reports/recommended                                какие отчёты выводить и как часто
```

Отчёты строятся из сохранённого результата: для Монте-Карло — по образцовому
прогону, распределения сроков, стоимости и денег — по всем прогонам. Подробно
— docs/EVM_AND_FINANCE.md.

## Стыковка движков

```
GET  /interop/contract     описание контракта и возможностей движка
POST /interop/run          прогон по контракту для сохранённой модели
POST /interop/run-raw      прогон по внешнему запросу (серверная сторона)
POST /interop/certify      проверка движка на соответствие контракту (editor)
POST /interop/compare      сравнение нашего движка с внешним (editor)
```

Контракт «модель + политика + сиды → траектория, экономика, журналы» описан в
INTEROP.md, разграничение предметов — в SUBJECT_DELIMITATION.md.

## Прочее

```
GET /audit[?enterprise_id=&limit=]   журнал изменений (кто/что/когда)
GET /health
```

Rate limiting: импортные ручки — 30/мин, запуск симуляций/оптимизаций — 60/мин
на актора.
