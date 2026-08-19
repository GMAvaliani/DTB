# DTB — ERP Integration

Принцип: **ERP — источник атомарных данных** (виды 4–7), платформа не подменяет
ERP. Конвейер:

```
ERP → Connector → Normalization → Domain Model → Digital Twin
```

## Интерфейс коннектора

```python
class ERPConnector(Protocol):
    system: str                                   # "1c", "galaktika", "mock_1c"…
    def fetch(self) -> dict[str, list[dict]]: ... # {object_type: [raw rows]}
```

`sync_connector(model, connector)` («агент-Актуализатор»):

1. каждый raw-объект сохраняется как `ERPObject` (вид 7, atomic data);
2. распознанные типы нормализуются в доменную модель и получают `ERPMapping`
   (вид 8): `material_stock` → начальные запасы полезностей; `employee` →
   сотрудники; `equipment` → оборудование; `org_unit` → орг-единицы;
   `kpi_actual` → серии факта;
3. незнакомые типы остаются raw-объектами (не выдумываем семантику);
4. неизвестные модели полезности/KPI — предупреждение в `ImportReport`, а не
   тихое создание;
5. `initial_state.source`/`as_of` фиксируют происхождение актуализации.

## Поставляемые коннекторы

- **MockERPConnector** — реалистичный набор данных, идущий через настоящий
  конвейер нормализации (для демо и тестов; честная реализация интерфейса, а не
  фиктивная интеграция).
- **RESTConnector** — generic REST: карта `object_type → {url, items_path,
  field_map}`, заголовки авторизации, таймауты.
- **CSV / Excel / JSON импорт** — см. ниже.

Адаптеры 1С/Галактика/MES/WMS/HRM реализуют тот же интерфейс `fetch()`
(планируются, см. ROADMAP; фиктивных заглушек в кодовой базе нет).

## Excel-книга (ActBasedDigTwinFrmwrk-структура, виды 1–8)

Листы: `goals, seds, utility_types, kpis, plans, actuals, org_units, employees,
equipment, resources, event_kinds, erp_objects, erp_mappings, initial_state`
(принимаются и русские названия листов: «Цели», «СЭДы», «Факт», «Планы»…).

Компактный синтаксис ячеек:

- потоки/ресурсы: `steel:2; energy:0.5`;
- длительность: `fixed:4`, `uniform:2..6`, `triangular:2..6@4`, `normal:5,1`,
  `lognormal:1,0.5`, `exponential:5`;
- триггеры: `on_event:customer_order`, `on_sed_completed:production`,
  `at_time:5`, `recurring:24@8`;
- целевые показатели: `revenue:>=:2000000[:deadline_hours]`;
- сложные структуры (ветви исходов, эффекты событий) — JSON в ячейке.

`GET /api/import/template` отдаёт пустую книгу-шаблон;
`GET /api/enterprises/{eid}/export/excel` — модель в этой же структуре
(экспорт ↔ импорт дают эквивалентную модель — проверено тестами).

## Отчёт импорта

`ImportReport`: created/updated по типам, ошибки с номерами строк, предупреждения.
Импорт с ошибками не сохраняет модель; каждый импорт попадает в audit trail.
Версионирование — явным снимком (`POST /versions`) до/после импорта.
