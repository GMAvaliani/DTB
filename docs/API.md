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
POST /demo                                       демо-предприятие + сценарии
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
GET  /simulations/{id}/stream      SSE-прогресс (text/event-stream)
GET  /simulations/compare?ids=a,b  сравнение (первый — baseline)
```

Запуск блокируется с 422, если валидация модели или сценария имеет критические
ошибки.

## Прогноз и оптимизация

```
GET  /forecasts?scenario_id=[&simulation_id=]   Вид 3: факт/план/прогноз+CI,
                                                P(цели), bottlenecks, ресурсы
GET  /forecasts/deviations?scenario_id=         причины отклонений
POST /optimizations                {scenario_id, variables[], objective_kpi_id
                                    | objective_expression, direction,
                                    constraints[], runs_per_evaluation, seed,
                                    optimizer} → 202
GET  /optimizations/{id}
```

## Прочее

```
GET /audit[?enterprise_id=&limit=]   журнал изменений (кто/что/когда)
GET /health
```

Rate limiting: импортные ручки — 30/мин, запуск симуляций/оптимизаций — 60/мин
на актора.
