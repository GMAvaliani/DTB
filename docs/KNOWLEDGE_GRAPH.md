# DTB — Knowledge Graph

`dtb.graph` — типизированный граф знаний над цифровым двойником (Виды 1, 2, 8).

## Узлы

По одному узлу на каждую сущность модели, с неймспейсом в id:
`enterprise:*, goal:*, sed:*, utility_type:*, resource:*, org_unit:*,
employee:*, equipment:*, erp_object:*, event_kind:*, kpi:*` + произвольные
пользовательские узлы (`custom`).

## Отношения

`HAS_GOAL, PARENT_OF, IMPLEMENTS (СЭД→цель), REQUIRES, PRODUCES, CONSUMES,
TRIGGERS (событие→СЭД; СЭД→СЭД по завершению), EXECUTED_BY (СЭД→орг-единица),
USES_RESOURCE, USES_EQUIPMENT, PRODUCES_UTILITY, DEPENDS_ON, RESPONSIBLE_FOR
(орг-единица→цель), REPRESENTS, MAPPED_TO_ERP, AFFECTS (событие→ресурс/полезность),
CAUSES (производитель→потребитель через общую полезность), MEMBER_OF,
CONTRIBUTES_TO (сотрудник/оборудование→ресурсный пул), MEASURED_BY (цель→KPI)`.

## Проекция

`project_model(model)` детерминированно строит граф из доменной модели; связи с
атрибутами (количества потоков, вероятности ветвей, дельты ёмкости).
Пользовательские узлы/связи, добавленные через API, хранятся отдельно
(в metadata предприятия, версионируются вместе с моделью) и мержатся поверх
проекции — проекцию можно пересчитывать в любой момент.

## Запросы

- `neighborhood(node, depth, types)` — BFS-окрестность;
- `paths(source, target, max_length, limit)` — направленные пути;
- `impact(node)` — **нисходящее причинное замыкание**: «что произойдёт, если
  изменить X». Вперёд идут TRIGGERS/PRODUCES/CAUSES/AFFECTS/IMPLEMENTS/
  MEASURED_BY/CONTRIBUTES_TO; «против шерсти» — CONSUMES/USES_RESOURCE/
  USES_EQUIPMENT/REQUIRES (потребители полезности, пользователи ресурса).
  Возвращает подграф + цепочки влияния вида
  `ресурс → СЭД → полезность → цель → KPI`;
- `dependencies(node)` — восходящее замыкание (от чего зависит X);
- `stats()`, `to_dict(types)` — статистика и Graph JSON.

## API

`GET /api/enterprises/{eid}/graph[?types=…]`,
`GET …/graph/neighborhood/{node}?depth=`, `GET …/graph/impact/{node}`,
`GET …/graph/dependencies/{node}`, `GET …/graph/paths?source=&target=`,
`POST …/graph/entities`, `POST …/graph/relations` (archive — через флаг узла),
`GET …/export/graph-json`.

## Хранение

In-memory индекс + сериализация в реляционные структуры (ADR-0002); интерфейс
`KnowledgeGraph` — точка подмены на Neo4j/JanusGraph при росте.
