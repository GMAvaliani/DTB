/* DTB SPA views. All domain logic lives on the server; views render API data. */
"use strict";

/* ------------------------------ shared helpers ---------------------------- */

function card(title, ...children) {
  const c = el("div", { class: "card" });
  if (title) c.append(el("h3", null, title));
  // flatten: views legitimately pass arrays of nodes (rows, steps, issue lists)
  children.flat().forEach(child => {
    if (child !== null && child !== undefined) c.append(child);
  });
  return c;
}

function dataTable(headers, rows, onRowClick) {
  const thead = el("thead", null, el("tr", null, headers.map(h => el("th", null, h))));
  const tbody = el("tbody");
  rows.forEach(r => {
    const tr = el("tr", { class: onRowClick ? "clickable" : "" }, r.cells.map(c => el("td", null, c)));
    if (onRowClick) tr.addEventListener("click", () => onRowClick(r, tr));
    tbody.append(tr);
  });
  return el("div", { class: "scroll-x" }, el("table", null, thead, tbody));
}

function needEnterprise(main) {
  if (State.eid) return false;
  main.innerHTML = "";
  main.append(card("Предприятие не выбрано",
    el("p", null, "Создайте предприятие или выберите его в списке слева."),
    el("div", { class: "btn-row" },
      el("button", { class: "primary", onclick: () => location.hash = "#/dashboard" }, "К списку предприятий"))));
  return true;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

async function entityEditor(etype, template, existing, onDone) {
  jsonEditorModal(
    existing ? `Редактирование: ${etype} ${existing.id}` : `Новый объект: ${etype}`,
    existing || template,
    async data => {
      await API.post(`/enterprises/${State.eid}/entities/${etype}`, data);
      toast("Сохранено", "ok");
      onDone && onDone();
    });
}

const TEMPLATES = {
  goal: { id: "new_goal", name: "Новая цель", parent_id: null, responsible_org_unit_id: null,
    priority: 100, target_metrics: [{ kpi_id: "revenue", target_value: 100000, comparison: ">=" }] },
  utility_type: { id: "new_utility", name: "Новая полезность", kind: "product", unit: "шт" },
  sed: { id: "new_sed", name: "Новый СЭД", goal_id: null, org_unit_id: null,
    inputs: [{ utility_type_id: "", quantity: 1 }], outputs: [{ utility_type_id: "", quantity: 1 }],
    resources: [{ resource_id: "", quantity: 1 }],
    duration: { kind: "fixed", value: 4 },
    triggers: [{ kind: "on_event", event_kind_id: "" }], priority: 100 },
  resource: { id: "new_resource", name: "Новый ресурс", kind: "equipment", capacity: 1, unit: "ед" },
  org_unit: { id: "new_unit", name: "Новая орг-единица", kind: "department", parent_id: null },
  employee: { id: "new_employee", name: "Новый сотрудник", org_unit_id: null, position: "", skills: [], resource_id: null },
  equipment: { id: "new_equipment", name: "Новое оборудование", tech_place: "", org_unit_id: null, resource_id: null },
  event_kind: { id: "new_event", name: "Новое событие", category: "external",
    stream: { kind: "poisson", rate_per_hour: 0.1 },
    effects: [{ kind: "add_utility", utility_type_id: "", quantity_expression: "1" }] },
  kpi: { id: "new_kpi", name: "Новый KPI", expression: "produced('money')", unit: "" },
  erp_object: { id: "erp:obj:1", name: "ERP-объект", system: "manual", object_type: "material", external_id: "1", payload: {} },
};

/* -------------------------------- dashboard ------------------------------- */

async function vDashboard(main) {
  const list = await API.get("/enterprises");
  State.enterprises = list;
  renderEnterprisePicker();
  main.append(el("h1", null, "Предприятия"),
    el("p", { class: "subtitle" }, "Цифровые двойники, доступные на платформе"));
  const rows = list.map(e => ({
    cells: [e.id, e.name || "—", e.updated_at],
    id: e.id,
  }));
  main.append(card(null, list.length
    ? dataTable(["ID", "Название", "Обновлено"], rows, r => State.setEnterprise(r.id))
    : el("p", { class: "muted" }, "Пока нет ни одного предприятия.")));
  main.append(el("div", { class: "btn-row" },
    el("button", { class: "primary", onclick: () => {
      jsonEditorModal("Новое предприятие", {
        id: "my_company", name: "Моё предприятие", description: "", planning_horizon_hours: 720,
      }, async data => {
        await API.post("/enterprises", data);
        toast("Предприятие создано", "ok");
        await State.loadEnterprises();
        State.setEnterprise(data.id);
      });
    } }, "Создать предприятие"),
    el("button", { onclick: async () => {
      try {
        const r = await API.post("/demo");
        toast(`Демо-модель создана: ${r.enterprise_id}`, "ok");
        await State.loadEnterprises();
        State.setEnterprise(r.enterprise_id);
      } catch (e) { showError(e); }
    } }, "Создать демо-модель «Демо-Завод»")));
}

/* ------------------------------- enterprise ------------------------------- */

async function vEnterprise(main) {
  if (needEnterprise(main)) return;
  const [info, validation, dq, scenarios, sims] = await Promise.all([
    API.get(`/enterprises/${State.eid}`),
    API.get(`/enterprises/${State.eid}/validation`),
    API.get(`/enterprises/${State.eid}/data-quality`),
    API.get(`/scenarios?enterprise_id=${State.eid}`),
    API.get(`/simulations?enterprise_id=${State.eid}`),
  ]);
  const e = info.enterprise;
  main.append(el("h1", null, e.name || e.id),
    el("p", { class: "subtitle" }, e.description || "—"));
  const counts = info.counts;
  main.append(el("div", { class: "kpi-row" },
    kpiTile("Целей", counts.goal), kpiTile("СЭД", counts.sed),
    kpiTile("Полезностей", counts.utility_type), kpiTile("Ресурсов", counts.resource),
    kpiTile("Событий", counts.event_kind), kpiTile("Сценариев", scenarios.length),
    kpiTile("Симуляций", sims.length),
    kpiTile("Версия модели", e.model_version)));

  const vBadge = validation.ok_to_simulate
    ? el("span", { class: "badge ok" }, "модель готова к симуляции")
    : el("span", { class: "badge err" }, `критических ошибок: ${validation.critical}`);
  const issues = validation.issues.slice(0, 30).map(i =>
    el("div", { class: "small" },
      el("span", { class: `badge ${i.severity === "critical" ? "err" : i.severity === "warning" ? "warn" : "muted"}` }, i.severity),
      ` [${i.entity_type}:${i.entity_id}] ${i.message}`));
  const dqBadge = el("span", { class: `badge ${dq.score > 90 ? "ok" : dq.score > 70 ? "warn" : "err"}` },
    `качество данных: ${dq.score}%`);
  main.append(el("div", { class: "row" },
    el("div", { class: "col" }, card("Валидация модели", vBadge,
      issues.length ? el("div", { style: "margin-top:8px" }, issues) : el("p", { class: "muted small", style: "margin-top:8px" }, "Замечаний нет"))),
    el("div", { class: "col" }, card("Качество данных", dqBadge,
      el("div", { style: "margin-top:8px" }, dq.issues.slice(0, 15).map(i =>
        el("div", { class: "small" }, el("span", { class: `badge ${i.severity === "error" ? "err" : "warn"}` }, i.check), ` ${i.message}`)))))));

  const steps = [
    ["Создать предприятие", true, "#/dashboard"],
    ["Определить цели", counts.goal > 0, "#/goals"],
    ["Построить СЭДы", counts.sed > 0, "#/seds"],
    ["Связать СЭД с полезностями", counts.utility_type > 0 && counts.sed > 0, "#/seds"],
    ["Загрузить оргструктуру", counts.org_unit > 0, "#/org"],
    ["Загрузить ERP-объекты", counts.erp_object > 0, "#/erp"],
    ["Связать модель с ERP", counts.erp_object > 0, "#/erp"],
    ["Загрузить фактическое состояние", Object.keys(info.initial_state.utility_stocks || {}).length > 0, "#/import"],
    ["Создать сценарий", scenarios.length > 0, "#/scenarios"],
    ["Запустить симуляцию", sims.length > 0, "#/simulate"],
    ["Получить прогноз", sims.some(s => s.status === "completed"), "#/forecast"],
    ["Изменить управляющие воздействия (what-if)", scenarios.some(s => s.base_scenario_id), "#/scenarios"],
    ["Сравнить сценарии", sims.filter(s => s.status === "completed").length > 1, "#/compare"],
    ["Запустить оптимизацию", sims.some(s => s.kind === "optimization"), "#/optimize"],
  ];
  main.append(card("Рабочий процесс цифрового двойника",
    steps.map(([label, done, href], i) => el("div", { class: `wf-step ${done ? "done" : ""}` },
      el("span", { class: "n" }, done ? "✓" : String(i + 1)),
      el("a", { href, style: "color:inherit" }, label)))));
}

function kpiTile(label, value, cls) {
  return el("div", { class: `kpi-tile ${cls || ""}` },
    el("div", { class: "v" }, fmt(value)), el("div", { class: "l" }, label));
}

/* --------------------------------- goals ---------------------------------- */

async function vGoals(main) {
  if (needEnterprise(main)) return;
  const [goals, kpis, orgs] = await Promise.all([
    API.get(`/enterprises/${State.eid}/entities/goal`),
    API.get(`/enterprises/${State.eid}/entities/kpi`),
    API.get(`/enterprises/${State.eid}/entities/org_unit`),
  ]);
  main.append(el("h1", null, "Дерево целей"),
    el("p", { class: "subtitle" }, "Структура целей = иерархия СЭД = центры ответственности (Вид 1)"));
  const byParent = {};
  goals.forEach(g => (byParent[g.parent_id || ""] = byParent[g.parent_id || ""] || []).push(g));
  const detail = el("div", { class: "col" });
  const orgName = id => (orgs.find(o => o.id === id) || {}).name || id || "—";

  function goalNode(g) {
    const li = el("li");
    const node = el("span", { class: "node", onclick: () => showGoal(g) },
      el("b", null, g.name || g.id), " ",
      g.target_metrics.length ? el("span", { class: "badge info" }, `${g.target_metrics.length} KPI`) : null,
      g.responsible_org_unit_id ? el("span", { class: "badge muted", style: "margin-left:4px" }, orgName(g.responsible_org_unit_id)) : null);
    li.append(node);
    const children = (byParent[g.id] || []).sort((a, b) => a.priority - b.priority);
    if (children.length) li.append(el("ul", null, children.map(goalNode)));
    return li;
  }

  function showGoal(g) {
    detail.innerHTML = "";
    detail.append(card(`Цель: ${g.name || g.id}`,
      el("p", { class: "small muted" }, g.description || ""),
      dataTable(["KPI", "Цель", "Дедлайн"], g.target_metrics.map(tm => ({
        cells: [tm.kpi_id, `${tm.comparison} ${fmt(tm.target_value)}`, tm.deadline_hours ? fmtT(tm.deadline_hours) : "конец горизонта"],
      }))),
      el("p", { class: "small", style: "margin-top:8px" }, "Реализуют СЭДы: ",
        g.sed_ids.length ? g.sed_ids.join(", ") : el("span", { class: "muted" }, "— (связь через goal_id СЭДа)")),
      el("div", { class: "btn-row" },
        el("button", { class: "small", onclick: () => entityEditor("goal", null, g, () => route()) }, "Редактировать"),
        el("button", { class: "small", onclick: () => { location.hash = "#/graph"; sessionStorage.setItem("dtb_focus", "goal:" + g.id); } }, "В графе знаний"),
        el("button", { class: "small danger", onclick: () => confirmModal(`Удалить цель ${g.id}?`, async () => {
          await API.del(`/enterprises/${State.eid}/entities/goal/${g.id}`); toast("Удалено", "ok"); route();
        }) }, "Удалить"))));
  }

  const roots = (byParent[""] || []).sort((a, b) => a.priority - b.priority);
  main.append(el("div", { class: "row" },
    el("div", { class: "col" }, card(null,
      roots.length ? el("div", { class: "tree" }, el("ul", null, roots.map(goalNode)))
        : el("p", { class: "muted" }, "Целей пока нет."),
      el("div", { class: "btn-row" },
        el("button", { class: "primary", onclick: () => entityEditor("goal", TEMPLATES.goal, null, () => route()) }, "Добавить цель"),
        el("button", { onclick: () => entityEditor("kpi", TEMPLATES.kpi, null, () => route()) }, "Добавить KPI")),
      el("h2", null, "KPI"),
      dataTable(["ID", "Название", "Выражение", "Ед."], kpis.map(k => ({
        cells: [k.id, k.name, el("span", { class: "mono" }, k.expression), k.unit], k,
      })), r => entityEditor("kpi", null, r.k, () => route())))),
    detail));
}

/* ------------------------------- СЭД designer ------------------------------ */

async function vSeds(main) {
  if (needEnterprise(main)) return;
  const graph = await API.get(`/enterprises/${State.eid}/graph?types=sed,utility_type,event_kind`);
  main.append(el("h1", null, "СЭД / Полезности"),
    el("p", { class: "subtitle" },
      "Причинно-следственная модель: события → СЭДы → полезности (Вид 2). Узлы можно перетаскивать; клик — свойства."));
  const nodes = graph.nodes;
  const edges = graph.relations.filter(r => ["TRIGGERS", "PRODUCES", "CONSUMES"].includes(r.type))
    .map(r => r.type === "CONSUMES" ? { ...r, source: r.target, target: r.source } : r);
  const detail = el("div", { class: "col", style: "max-width:380px" });
  const saved = State.nodePositions("seds");
  const view = graphView({
    nodes, edges, layout: "layered", height: 540,
    positions: saved,
    onMove: pos => State.saveNodePositions("seds", pos),
    onSelect: n => showNode(n),
  });
  main.append(graphLegend(["event_kind", "sed", "utility_type"]));
  main.append(el("div", { class: "row" }, el("div", { class: "col" }, view), detail));

  async function showNode(n) {
    detail.innerHTML = "";
    try {
      const explain = await API.get(`/enterprises/${State.eid}/ai/explain/${n.id}`);
      const etype = n.type, ref = n.ref_id;
      detail.append(card(`${TYPE_LABELS[etype] || etype}: ${n.label}`,
        el("div", null, explain.statements.slice(0, 10).map(s => el("div", { class: "small" }, "• " + s))),
        explain.incoming.length ? el("details", null, el("summary", null, "Входящие связи"),
          explain.incoming.slice(0, 10).map(s => el("div", { class: "small" }, "• " + s))) : null,
        el("p", { class: "small muted", style: "margin-top:6px" }, `Влияет на ${explain.impact_size} сущностей`),
        el("div", { class: "btn-row" },
          ref && etype !== "enterprise" ? el("button", { class: "small", onclick: async () => {
            const entity = await API.get(`/enterprises/${State.eid}/entities/${etype}/${ref}`);
            entityEditor(etype, null, entity, () => route());
          } }, "Редактировать") : null,
          el("button", { class: "small", onclick: () => { sessionStorage.setItem("dtb_focus", n.id); location.hash = "#/graph"; } }, "Анализ влияния"))));
    } catch (e) { showError(e); }
  }

  main.append(card("Создание элементов модели", el("div", { class: "btn-row" },
    el("button", { class: "primary", onclick: () => entityEditor("sed", TEMPLATES.sed, null, () => route()) }, "Новый СЭД"),
    el("button", { onclick: () => entityEditor("utility_type", TEMPLATES.utility_type, null, () => route()) }, "Новая полезность"),
    el("button", { onclick: () => entityEditor("event_kind", TEMPLATES.event_kind, null, () => route()) }, "Новое событие"),
    el("button", { onclick: async () => {
      const seds = await API.get(`/enterprises/${State.eid}/entities/sed`);
      modal("Список СЭД", dataTable(["ID", "Название", "Входы", "Выходы", "Триггеры"], seds.map(s => ({
        cells: [s.id, s.name,
          s.inputs.map(f => `${f.utility_type_id}:${f.quantity}`).join("; "),
          (s.outputs || []).map(f => `${f.utility_type_id}:${f.quantity}`).join("; ") ||
            (s.outcome_branches || []).map(b => b.label).join("/"),
          s.triggers.map(t => t.kind).join("; ")], s,
      })), r => entityEditor("sed", null, r.s, () => route())), []);
    } }, "Таблица СЭД"))));
}

/* --------------------------------- events --------------------------------- */

async function vEvents(main) {
  if (needEnterprise(main)) return;
  const events = await API.get(`/enterprises/${State.eid}/entities/event_kind`);
  main.append(el("h1", null, "События внешней среды (НП-события)"),
    el("p", { class: "subtitle" }, "Неопределённые (пуассоновские потоки), плановые/регламентные и сценарные события"));
  main.append(card(null, dataTable(
    ["ID", "Название", "Категория", "Поток", "Эффекты"],
    events.map(ev => ({
      cells: [ev.id, ev.name,
        el("span", { class: "badge info" }, ev.category),
        ev.stream.kind === "poisson" ? `Пуассон, λ=${ev.stream.rate_per_hour}/ч`
          : ev.stream.kind === "scheduled" ? `по расписанию (${ev.stream.interval ? "каждые " + fmtT(ev.stream.interval) : ev.stream.times.length + " моментов"})`
          : ev.stream.kind === "external" ? "внешняя модель (URI)" : "только из сценария",
        ev.effects.map(e => e.kind).join(", ") || "—"], ev,
    })), r => entityEditor("event_kind", null, r.ev, () => route())),
    el("div", { class: "btn-row" },
      el("button", { class: "primary", onclick: () => entityEditor("event_kind", TEMPLATES.event_kind, null, () => route()) }, "Добавить событие"))));
}

/* ----------------------------------- org ----------------------------------- */

async function vOrg(main) {
  if (needEnterprise(main)) return;
  const [orgs, employees] = await Promise.all([
    API.get(`/enterprises/${State.eid}/entities/org_unit`),
    API.get(`/enterprises/${State.eid}/entities/employee`),
  ]);
  main.append(el("h1", null, "Организационная структура"),
    el("p", { class: "subtitle" }, "Орг-единицы, штатное расписание, сотрудники, центры ответственности (Вид 4)"));
  const byParent = {};
  orgs.forEach(o => (byParent[o.parent_id || ""] = byParent[o.parent_id || ""] || []).push(o));
  function orgNode(o) {
    const emps = employees.filter(e => e.org_unit_id === o.id);
    const li = el("li", null, el("span", { class: "node", onclick: () => entityEditor("org_unit", null, o, () => route()) },
      el("b", null, o.name || o.id), " ", el("span", { class: "badge muted" }, o.kind),
      emps.length ? el("span", { class: "badge info", style: "margin-left:4px" }, `${emps.length} чел`) : null));
    const children = byParent[o.id] || [];
    if (children.length) li.append(el("ul", null, children.map(orgNode)));
    return li;
  }
  main.append(el("div", { class: "row" },
    el("div", { class: "col" }, card("Структура",
      el("div", { class: "tree" }, el("ul", null, (byParent[""] || []).map(orgNode))),
      el("div", { class: "btn-row" },
        el("button", { class: "primary", onclick: () => entityEditor("org_unit", TEMPLATES.org_unit, null, () => route()) }, "Добавить орг-единицу")))),
    el("div", { class: "col" }, card("Сотрудники",
      dataTable(["ФИО", "Подразделение", "Должность", "Ресурсный пул"], employees.map(e => ({
        cells: [e.name, (orgs.find(o => o.id === e.org_unit_id) || {}).name || "—", e.position, e.resource_id || "—"], e,
      })), r => entityEditor("employee", null, r.e, () => route())),
      el("div", { class: "btn-row" },
        el("button", { class: "primary", onclick: () => entityEditor("employee", TEMPLATES.employee, null, () => route()) }, "Добавить сотрудника"))))));
}

/* -------------------------------- resources -------------------------------- */

async function vResources(main) {
  if (needEnterprise(main)) return;
  const [resources, equipment] = await Promise.all([
    API.get(`/enterprises/${State.eid}/entities/resource`),
    API.get(`/enterprises/${State.eid}/entities/equipment`),
  ]);
  main.append(el("h1", null, "Ресурсы и оборудование"),
    el("p", { class: "subtitle" }, "Ресурсные пулы, используемые СЭДами; оборудование и технологические места (Виды 5–6)"));
  main.append(el("div", { class: "row" },
    el("div", { class: "col" }, card("Ресурсы",
      dataTable(["ID", "Название", "Тип", "Ёмкость", "Ед.", "Стоимость/ч"], resources.map(r => ({
        cells: [r.id, r.name, r.kind, fmt(r.capacity), r.unit, fmt(r.cost_per_hour)], r,
      })), row => entityEditor("resource", null, row.r, () => route())),
      el("div", { class: "btn-row" },
        el("button", { class: "primary", onclick: () => entityEditor("resource", TEMPLATES.resource, null, () => route()) }, "Добавить ресурс")))),
    el("div", { class: "col" }, card("Оборудование / технологические места",
      dataTable(["ID", "Название", "Тех. место", "Ресурсный пул", "Статус"], equipment.map(q => ({
        cells: [q.id, q.name, q.tech_place || "—", q.resource_id || "—", q.status], q,
      })), row => entityEditor("equipment", null, row.q, () => route())),
      el("div", { class: "btn-row" },
        el("button", { class: "primary", onclick: () => entityEditor("equipment", TEMPLATES.equipment, null, () => route()) }, "Добавить оборудование"))))));
}

/* ----------------------------------- ERP ----------------------------------- */

async function vErp(main) {
  if (needEnterprise(main)) return;
  const model = await API.get(`/enterprises/${State.eid}/model`);
  const objects = Object.values(model.erp_objects || {});
  const mappings = model.erp_mappings || [];
  main.append(el("h1", null, "ERP-объекты и связи"),
    el("p", { class: "subtitle" }, "Атомарные данные из учётных систем (Вид 7) и связи макро-модели с ERP (Вид 8)"));
  main.append(el("div", { class: "btn-row" },
    el("button", { class: "primary", onclick: async () => {
      const report = await API.post(`/enterprises/${State.eid}/erp/sync-mock`);
      toast(`Синхронизация: создано ${JSON.stringify(report.created)}`, "ok");
      route();
    } }, "Синхронизировать с mock-ERP (агент-Актуализатор)"),
    el("button", { onclick: () => entityEditor("erp_object", TEMPLATES.erp_object, null, () => route()) }, "Добавить ERP-объект вручную")));
  main.append(el("div", { class: "row" },
    el("div", { class: "col" }, card(`ERP-объекты (${objects.length})`,
      dataTable(["Система", "Тип", "Внешний ID", "Название"], objects.slice(0, 200).map(o => ({
        cells: [o.system, o.object_type, o.external_id, o.name],
      }))))),
    el("div", { class: "col" }, card(`Связи модель ↔ ERP (${mappings.length})`,
      dataTable(["Сущность", "ID", "ERP-объект", "Тип связи"], mappings.slice(0, 200).map(m => ({
        cells: [m.entity_type, m.entity_id, m.erp_object_id, m.kind],
      })))))));
  main.append(card("Начальное состояние (запасы полезностей)",
    dataTable(["Полезность", "Количество"], Object.entries(model.initial_state.utility_stocks || {}).map(([k, v]) => ({
      cells: [k, fmt(v)],
    }))),
    el("p", { class: "small muted" }, `Источник: ${model.initial_state.source || "manual"}; на момент: ${model.initial_state.as_of || "—"}`),
    el("div", { class: "btn-row" }, el("button", { class: "small", onclick: () => {
      jsonEditorModal("Начальное состояние", model.initial_state, async data => {
        await API.put(`/enterprises/${State.eid}/initial-state`, data);
        toast("Сохранено", "ok"); route();
      });
    } }, "Редактировать"))));
}

/* -------------------------------- scenarios -------------------------------- */

async function vScenarios(main) {
  if (needEnterprise(main)) return;
  const scenarios = await API.get(`/scenarios?enterprise_id=${State.eid}`);
  main.append(el("h1", null, "Сценарии"),
    el("p", { class: "subtitle" }, "Сценарий = горизонт + начальное состояние + НП-события + управляющие воздействия + целевая функция"));
  const rows = scenarios.map(s => ({
    cells: [s.id, s.name, s.base_scenario_id ? el("span", { class: "badge info" }, "what-if от " + s.base_scenario_id) : "базовый",
      fmtT(s.horizon_hours) || "по умолчанию", s.monte_carlo_runs, s.seed ?? "—",
      el("span", null,
        el("button", { class: "small", onclick: ev => { ev.stopPropagation(); openWhatIf(s); } }, "What-if"),
        " ",
        el("button", { class: "small", onclick: ev => { ev.stopPropagation(); location.hash = "#/simulate"; sessionStorage.setItem("dtb_scn", s.id); } }, "Симулировать"))],
    s,
  }));
  main.append(card(null,
    dataTable(["ID", "Название", "Происхождение", "Горизонт", "MC-прогонов", "Seed", ""], rows,
      r => jsonEditorModal(`Сценарий ${r.s.id}`, r.s, async data => {
        await API.put(`/scenarios/${r.s.id}`, data); toast("Сохранено", "ok"); route();
      })),
    el("div", { class: "btn-row" },
      el("button", { class: "primary", onclick: () => {
        jsonEditorModal("Новый сценарий", {
          name: "Новый сценарий", enterprise_id: State.eid, seed: 42, monte_carlo_runs: 20,
          event_rate_multipliers: {}, resource_capacity_overrides: {}, initial_stock_overrides: {},
        }, async data => {
          const r = await API.post("/scenarios", data);
          toast("Сценарий создан: " + r.id, "ok"); route();
        });
      } }, "Создать сценарий"))));

  function openWhatIf(s) {
    jsonEditorModal(`What-if на основе «${s.name}»`, {
      name: s.name + " (вариант)",
      description: "",
      event_rate_multipliers: { customer_order: 1.2 },
      resource_capacity_overrides: {},
      sed_duration_scales: {},
      initial_stock_overrides: {},
      parameter_overrides: {},
    }, async data => {
      const r = await API.post(`/scenarios/${s.id}/whatif`, data);
      toast("Создан what-if сценарий: " + r.id, "ok"); route();
    });
  }
}

/* -------------------------------- simulation -------------------------------- */

async function vSimulate(main) {
  if (needEnterprise(main)) return;
  const scenarios = await API.get(`/scenarios?enterprise_id=${State.eid}`);
  main.append(el("h1", null, "Симуляция"),
    el("p", { class: "subtitle" }, "Event-driven симуляция в непрерывном модельном времени (моменты переключения = НП-события и завершения СЭД)"));
  if (!scenarios.length) {
    main.append(card(null, el("p", null, "Сначала создайте сценарий."),
      el("div", { class: "btn-row" }, el("button", { class: "primary", onclick: () => location.hash = "#/scenarios" }, "К сценариям"))));
    return;
  }
  const scnSel = el("select", null, scenarios.map(s => el("option", { value: s.id }, `${s.name} (${s.id})`)));
  const preset = sessionStorage.getItem("dtb_scn");
  if (preset) { scnSel.value = preset; sessionStorage.removeItem("dtb_scn"); }
  const kindSel = el("select", null,
    el("option", { value: "monte_carlo" }, "Монте-Карло (N прогонов)"),
    el("option", { value: "single" }, "Один прогон"));
  const runsInp = el("input", { type: "number", value: 20, min: 1, max: 500, style: "width:90px" });
  const seedInp = el("input", { type: "number", value: 42, style: "width:120px" });
  const monitor = el("div");
  main.append(card("Запуск",
    el("div", { class: "row" },
      el("div", null, el("label", null, "Сценарий"), scnSel),
      el("div", null, el("label", null, "Режим"), kindSel),
      el("div", null, el("label", null, "Прогонов"), runsInp),
      el("div", null, el("label", null, "Random seed"), seedInp)),
    el("div", { class: "btn-row" },
      el("button", { class: "primary", onclick: start }, "▶ Запустить симуляцию"))),
    monitor);

  async function start() {
    try {
      const body = {
        scenario_id: scnSel.value, kind: kindSel.value,
        runs: parseInt(runsInp.value) || 1, seed: parseInt(seedInp.value),
      };
      const r = await API.post("/simulations", body);
      toast("Симуляция запущена: " + r.id, "ok");
      renderMonitor(r.id);
    } catch (e) { showError(e); }
  }

  function renderMonitor(simId) {
    monitor.innerHTML = "";
    const bar = el("div", { class: "progress-bar" }, el("div", { style: "width:0%" }));
    const stats = el("div", { class: "kpi-row" });
    const status = el("span", { class: "badge info" }, "запуск…");
    const controls = el("div", { class: "btn-row" },
      el("button", { class: "small", onclick: () => API.post(`/simulations/${simId}/pause`).catch(showError) }, "⏸ Пауза"),
      el("button", { class: "small", onclick: () => API.post(`/simulations/${simId}/resume`).catch(showError) }, "▶ Продолжить"),
      el("button", { class: "small danger", onclick: () => API.post(`/simulations/${simId}/cancel`).catch(showError) }, "✕ Отменить"),
      el("button", { class: "small", onclick: () => { sessionStorage.setItem("dtb_sim", simId); location.hash = "#/results"; } }, "К результатам"));
    monitor.append(card(`Монитор симуляции ${simId}`, status, el("div", { style: "margin:10px 0" }, bar), stats, controls));

    // events arrive incrementally (engine telemetry, then per-run counters), so
    // keep the latest known value of every metric instead of rendering one event
    const acc = {};
    function update(ev) {
      Object.assign(acc, ev);
      if (acc.status) status.textContent = acc.status;
      const p = acc.progress !== undefined ? acc.progress : null;
      if (p !== null) bar.firstChild.style.width = `${Math.round(p * 100)}%`;
      stats.innerHTML = "";
      if (acc.run !== undefined) stats.append(kpiTile("Прогон", `${acc.run}/${acc.runs}`));
      if (acc.time !== undefined) stats.append(kpiTile("Модельное время", fmtT(acc.time)));
      if (acc.events !== undefined) stats.append(kpiTile("НП-событий", acc.events));
      if (acc.moments !== undefined) stats.append(kpiTile("Моментов переключения", acc.moments));
      if (acc.waiting !== undefined) stats.append(kpiTile("СЭД ожидают", acc.waiting));
      if (acc.executing !== undefined) stats.append(kpiTile("СЭД выполняются", acc.executing));
      if (ev.done) {
        status.className = "badge " + (ev.status === "completed" ? "ok" : "err");
        toast("Симуляция завершена: " + ev.status, ev.status === "completed" ? "ok" : "err");
      }
    }

    if (!API.key && window.EventSource) {
      const source = new EventSource(`/api/simulations/${simId}/stream`);
      source.onmessage = m => { const ev = JSON.parse(m.data); update(ev); if (ev.done) source.close(); };
      source.onerror = () => source.close();
    } else {
      const timer = setInterval(async () => {
        try {
          const st = await API.get(`/simulations/${simId}`);
          update({ ...(st.progress || {}), status: st.status, done: !["pending", "running", "paused"].includes(st.status) });
          if (!["pending", "running", "paused"].includes(st.status)) clearInterval(timer);
        } catch (e) { clearInterval(timer); }
      }, 1000);
    }
  }
}

/* --------------------------------- results --------------------------------- */

async function simPicker(main, filterKinds, onPick) {
  const sims = (await API.get(`/simulations?enterprise_id=${State.eid}`))
    .filter(s => !filterKinds || filterKinds.includes(s.kind));
  const sel = el("select", null, sims.map(s => el("option", { value: s.id },
    `${s.created_at} · ${s.kind} · ${s.scenario_id} · ${s.status} (${s.id.slice(-6)})`)));
  const preset = sessionStorage.getItem("dtb_sim");
  if (preset && sims.some(s => s.id === preset)) sel.value = preset;
  sessionStorage.removeItem("dtb_sim");
  return { sims, sel };
}

async function vResults(main) {
  if (needEnterprise(main)) return;
  main.append(el("h1", null, "Результаты и таймлайн"),
    el("p", { class: "subtitle" }, "Trace симуляции: KPI-траектории, события, пулы СЭД, ресурсы, узкие места"));
  const { sims, sel } = await simPicker(main, ["single", "monte_carlo"]);
  if (!sims.length) { main.append(card(null, el("p", { class: "muted" }, "Симуляций ещё нет."))); return; }
  const content = el("div");
  main.append(card(null, el("label", null, "Симуляция"), sel,
    el("div", { class: "btn-row" }, el("button", { class: "primary", onclick: load }, "Показать"))), content);

  async function load() {
    content.innerHTML = "";
    const simId = sel.value;
    try {
      const [state, timeline, events, bottlenecks, econ, control, decisions, trace, obsErr] =
        await Promise.all([
          API.get(`/simulations/${simId}/state`),
          API.get(`/simulations/${simId}/timeline`),
          API.get(`/simulations/${simId}/events?limit=500`),
          API.get(`/simulations/${simId}/bottlenecks`),
          API.get(`/simulations/${simId}/economics`),
          API.get(`/simulations/${simId}/rejected-actions`),
          API.get(`/simulations/${simId}/decisions?limit=200`),
          API.get(`/simulations/${simId}/policy-trace?limit=200`),
          API.get(`/simulations/${simId}/observation-error`),
        ]);
      const tiles = Object.entries(state.final_kpis || {}).slice(0, 10)
        .map(([k, v]) => kpiTile(k, v));
      content.append(el("div", { class: "kpi-row" }, tiles));
      if (state.goal_achievement) {
        content.append(card("Достижение целей (в этом прогоне)",
          dataTable(["Цель", "Достигнута", "Показатели"], Object.entries(state.goal_achievement).map(([g, d]) => ({
            cells: [g, d.achieved ? el("span", { class: "badge ok" }, "да") : el("span", { class: "badge err" }, "нет"),
              d.metrics.map(mm => `${mm.kpi_id}: ${fmt(mm.value)} ${mm.comparison} ${fmt(mm.target)}`).join("; ")],
          })))));
      }
      // KPI charts
      const kpiIds = Object.keys(timeline.kpi_series);
      const colors = ["var(--chart1)", "var(--chart2)", "var(--chart3)", "var(--chart4)", "var(--chart5)", "var(--chart6)", "var(--chart7)", "var(--chart8)"];
      const kpiSel = el("select", null, kpiIds.map(k => el("option", { value: k }, k)));
      const chartBox = el("div", { class: "svg-wrap" });
      const drawKpi = () => {
        chartBox.innerHTML = "";
        chartBox.append(lineChart({ width: 860, height: 280, series: [
          { name: kpiSel.value, color: "#4f8ff7", points: timeline.kpi_series[kpiSel.value], markers: false }] }));
      };
      kpiSel.addEventListener("change", drawKpi);
      const poolChart = lineChart({ width: 860, height: 200, series: [
        { name: "ожидают", color: "#f5b83d", points: timeline.pool_series.map(p => [p[0], p[1]]) },
        { name: "выполняются", color: "#3ecf8e", points: timeline.pool_series.map(p => [p[0], p[2]]) }] });
      content.append(card("KPI-траектория", kpiSel, el("div", { style: "margin-top:8px" }, chartBox)),
        card("Пулы СЭД во времени", chartLegend([["ожидают", "#f5b83d"], ["выполняются", "#3ecf8e"]]),
          el("div", { class: "svg-wrap" }, poolChart)));
      drawKpi();
      // sed + resource stats
      content.append(el("div", { class: "row" },
        el("div", { class: "col" }, card("Статистика СЭД",
          dataTable(["СЭД", "Создано", "Завершено", "Приостановок", "Часы ожидания", "Пик очереди"],
            Object.entries(timeline.sed_stats).map(([sid, s]) => ({
              cells: [sid, s.created, s.completed, s.suspensions, fmt(s.waiting_hours), s.queue_peak],
            }))))),
        el("div", { class: "col" }, card("Ресурсы",
          dataTable(["Ресурс", "Загрузка", "Занято, ч", "Пик"],
            Object.entries(timeline.resource_stats).map(([rid, r]) => ({
              cells: [rid, `${(r.utilization * 100).toFixed(1)}%`, fmt(r.busy_hours), r.peak_allocated],
            })))))));
      // экономика деятельности
      const e = econ.economics || {};
      if (e.entries) {
        const cur = e.currency || "";
        content.append(card("Экономика деятельности",
          el("p", { class: "small muted" },
            `Дисконт ${(e.discount_rate_annual * 100).toFixed(1)}% годовых непрерывно` +
            (e.warmup_hours ? ` · разогрев ${e.warmup_hours} ч отброшен` : "") +
            ` · измерено с ${fmtT(e.measured_from)} по ${fmtT(e.measured_to)}`),
          el("div", { class: "kpi-row" },
            kpiTile(`NPV, ${cur}`, e.npv), kpiTile(`Итог (номинал), ${cur}`, e.net),
            kpiTile(`Доход, ${cur}`, e.income), kpiTile(`Расход, ${cur}`, e.expense)),
          dataTable(["Статья", "Доход", "Расход", "Итог", "Итог дисконтированный"],
            Object.entries(e.by_article || {}).map(([a, b]) => ({
              cells: [a, fmt(b.income), fmt(b.expense), fmt(b.net), fmt(b.discounted_net)],
            }))),
          Object.keys(e.by_kind || {}).length
            ? dataTable(["Вид начисления", "Итог", "Итог дисконтированный"],
                Object.entries(e.by_kind).map(([k, b]) => ({
                  cells: [{ rate: "ставка", one_time: "разовое", terminal: "остаток" }[k] || k,
                    fmt(b.net), fmt(b.discounted_net)],
                })))
            : null));
      }
      // контур управления: МР, политики, наблюдения, стоимость и отклонения
      if (decisions.decision_moments || trace.policy_firings || (control.rejected_actions || []).length) {
        const byLoop = trace.by_loop || {};
        content.append(card("Контур управления",
          el("div", { class: "kpi-row" },
            kpiTile("Моментов переключения", decisions.switching_moments),
            kpiTile("Моментов решений", decisions.decision_moments),
            kpiTile("Сработок политик", trace.policy_firings),
            kpiTile("Стоимость управления", (control.control_costs || {}).total)),
          Object.keys(byLoop).length
            ? dataTable(["Контур", "Вызовов политик", "Сработало"],
                Object.entries(byLoop).map(([loop, b]) => ({
                  cells: [{ regulation: "регулирование", organization: "организация",
                    development: "развитие" }[loop] || loop, b.calls, b.fired],
                })))
            : null,
          (control.rejected_actions || []).length
            ? dataTable(["Время", "Действие", "Причина"],
                control.rejected_actions.slice(0, 20).map(r => ({
                  cells: [fmtT(r.t), r.action, r.reason],
                })))
            : null,
          Object.keys(obsErr.observations || {}).length
            ? dataTable(["Наблюдение", "Режим", "Чтений", "Средняя |Y−Ŷ|", "Макс. |Y−Ŷ|"],
                Object.entries(obsErr.observations).map(([oid, o]) => ({
                  cells: [oid, o.mode, o.reads, fmt(o.mean_abs_error), fmt(o.max_abs_error)],
                })))
            : null));
      }
      // верификация прогона: инварианты на его собственных данных
      try {
        const ver = await API.get(`/simulations/${simId}/verification`);
        const failed = (ver.checks || []).filter(c => !c.passed);
        content.append(card("Верификация прогона",
          el("p", { class: "small muted" },
            `Проверок: ${ver.total}, не прошло: ${ver.failed}. ` +
            "Баланс полезностей, закон Литтла, учёт ресурсов, сходимость экономики."),
          el("div", null,
            ver.passed
              ? el("span", { class: "badge ok" }, "все инварианты выполнены")
              : el("span", { class: "badge err" }, `нарушено проверок: ${ver.failed}`)),
          (failed.length ? failed : (ver.checks || []).slice(0, 8)).length
            ? dataTable(["Проверка", "Ожидалось", "Получено", "Погрешность", "Итог"],
                (failed.length ? failed : (ver.checks || []).slice(0, 8)).map(c => ({
                  cells: [c.name, fmt(c.expected), fmt(c.observed),
                    fmt(c.absolute_error),
                    c.passed ? el("span", { class: "badge ok" }, "ок")
                      : el("span", { class: "badge err" }, "нарушено")],
                })))
            : null));
      } catch (e) { /* верификация необязательна для отображения результатов */ }
      // bottlenecks + event log
      content.append(el("div", { class: "row" },
        el("div", { class: "col" }, card("Узкие места (bottlenecks)",
          bottlenecks.length ? el("div", null, bottlenecks.slice(0, 12).map(b =>
            el("div", { class: "small", style: "padding:3px 0" },
              el("span", { class: "badge warn" }, b.type), " " + b.evidence)))
            : el("p", { class: "muted" }, "Не обнаружено"))),
        el("div", { class: "col" }, card(`Журнал событий (${events.event_log.length}${events.truncated ? " + " + events.truncated + " скрыто" : ""})`,
          el("div", { class: "log-view" }, events.event_log.map(ev =>
            el("div", { class: `le ${ev.type}` },
              el("span", { class: "t" }, fmtT(ev.t)),
              `[${ev.type}] ${ev.name}`,
              ev.details && ev.details.instance ? ` · ${ev.details.instance}` : "")))))));
    } catch (e) { showError(e); }
  }
  load();
}

/* --------------------------------- forecast -------------------------------- */

async function vForecast(main) {
  if (needEnterprise(main)) return;
  main.append(el("h1", null, "Прогноз (Вид 3 — основной выход двойника)"),
    el("p", { class: "subtitle" }, "Факт · план · прогноз с доверительными интервалами · вероятность достижения целей · причины отклонений"));
  const scenarios = await API.get(`/scenarios?enterprise_id=${State.eid}`);
  if (!scenarios.length) { main.append(card(null, el("p", { class: "muted" }, "Создайте сценарий и запустите симуляцию."))); return; }
  const sel = el("select", null, scenarios.map(s => el("option", { value: s.id }, s.name)));
  const content = el("div");
  main.append(card(null, el("label", null, "Сценарий"), sel,
    el("div", { class: "btn-row" }, el("button", { class: "primary", onclick: load }, "Построить прогноз"))), content);

  async function load() {
    content.innerHTML = "";
    try {
      const fc = await API.get(`/forecasts?scenario_id=${encodeURIComponent(sel.value)}`);
      content.append(el("p", { class: "small muted" },
        `Симуляция ${fc.simulation_id} · ${fc.runs} прогонов · seed ${fc.provenance.seed} · модель v${fc.provenance.model_version} (${fc.provenance.model_hash})`));
      // goals
      content.append(card("Вероятность достижения целей",
        el("div", { class: "kpi-row" }, Object.entries(fc.goals).map(([gid, g]) => {
          const p = g.achievement_probability;
          return kpiTile(g.name, p === null ? "—" : `${(p * 100).toFixed(0)}%`,
            p === null ? "" : p >= 0.8 ? "ok" : p >= 0.5 ? "warn" : "err");
        })),
        dataTable(["Цель", "KPI", "Цель KPI", "P(достижения)", "Ожидание", "P5–P95"],
          Object.entries(fc.goals).flatMap(([gid, g]) => g.metrics.map(mm => ({
            cells: [g.name, mm.kpi_id, `${mm.comparison} ${fmt(mm.target)}`,
              `${(mm.probability * 100).toFixed(0)}%`, fmt(mm.expected_value), `${fmt(mm.p5)} – ${fmt(mm.p95)}`],
          }))))));
      // KPI fan chart
      const kpiIds = Object.keys(fc.kpis);
      const kpiSel = el("select", null, kpiIds.map(k => el("option", { value: k }, fc.kpis[k].name || k)));
      const box = el("div");
      const draw = () => {
        box.innerHTML = "";
        const r = fc.kpis[kpiSel.value];
        const grid = r.forecast.grid;
        const series = [
          { name: "прогноз (среднее)", color: "#4f8ff7", width: 2.5, points: grid.map((t, i) => [t, r.forecast.mean[i]]) },
        ];
        if (r.plan_series.length) series.push({ name: "план", color: "#f5b83d", dash: "6 4", points: r.plan_series.map(p => [p.t, p.value]), markers: true });
        if (r.actual_series.length) series.push({ name: "факт (история)", color: "#3ecf8e", points: r.actual_series.map(p => [p.t, p.value]), markers: true });
        const bands = [
          { color: "#4f8ff7", opacity: .12, points: grid.map((t, i) => [t, r.forecast.p5[i], r.forecast.p95[i]]) },
          { color: "#4f8ff7", opacity: .22, points: grid.map((t, i) => [t, r.forecast.ci_low[i], r.forecast.ci_high[i]]) },
        ];
        box.append(chartLegend([["прогноз", "#4f8ff7"], ["план", "#f5b83d"], ["факт", "#3ecf8e"], ["P5–P95", "rgba(79,143,247,.3)"]]));
        box.append(el("div", { class: "svg-wrap" }, lineChart({ width: 880, height: 320, series, bands })));
        const dev = r.deviation_from_plan;
        box.append(el("div", { class: "kpi-row", style: "margin-top:10px" },
          kpiTile("Ожидаемый итог", `${fmt(r.expected_final)} ${r.unit}`),
          kpiTile("План (итог)", r.plan_final !== null ? fmt(r.plan_final) : "—"),
          dev !== null ? kpiTile("Отклонение от плана", fmt(dev), dev >= 0 ? "ok" : "err") : null,
          kpiTile("P5 / P95", `${fmt(r.final_distribution.p5)} / ${fmt(r.final_distribution.p95)}`)));
      };
      kpiSel.addEventListener("change", draw);
      content.append(card("Динамика показателя: факт → план → прогноз", kpiSel, el("div", { style: "margin-top:10px" }, box)));
      draw();
      // causes + resources
      content.append(el("div", { class: "row" },
        el("div", { class: "col" }, card("Ключевые причины отклонений / узкие места",
          fc.deviation_causes.length ? el("div", null, fc.deviation_causes.map(c => el("div", { class: "small", style: "padding:3px 0" }, "• " + c)))
            : el("p", { class: "muted" }, "Существенных причин не выявлено"))),
        el("div", { class: "col" }, card("Влияние ресурсов",
          dataTable(["Ресурс", "Загрузка", "Ёмкость", "Пик"], fc.resource_influence.map(r => ({
            cells: [r.name, `${(r.utilization * 100).toFixed(0)}%`, fmt(r.capacity), r.peak_allocated],
          })))))));
    } catch (e) { showError(e); }
  }
}

/* --------------------------------- compare --------------------------------- */

async function vCompare(main) {
  if (needEnterprise(main)) return;
  main.append(el("h1", null, "Сравнение сценариев"),
    el("p", { class: "subtitle" }, "Сравнение результатов симуляций (первый выбранный — baseline)"));
  const sims = (await API.get(`/simulations?enterprise_id=${State.eid}`))
    .filter(s => s.status === "completed" && ["single", "monte_carlo"].includes(s.kind));
  if (sims.length < 2) { main.append(card(null, el("p", { class: "muted" }, "Нужно минимум две завершённые симуляции."))); return; }
  const checks = sims.map(s => {
    const cb = el("input", { type: "checkbox", value: s.id });
    return { s, cb, node: el("div", { class: "small" }, cb, ` ${s.created_at} · ${s.kind} · ${s.scenario_id} (${s.id.slice(-6)})`) };
  });
  const content = el("div");
  main.append(card("Выбор симуляций", checks.map(c => c.node),
    el("div", { class: "btn-row" }, el("button", { class: "primary", onclick: load }, "Сравнить"))), content);

  async function load() {
    const ids = checks.filter(c => c.cb.checked).map(c => c.s.id);
    if (ids.length < 2) { toast("Выберите минимум две симуляции", "err"); return; }
    content.innerHTML = "";
    try {
      const cmp = await API.get(`/simulations/compare?ids=${ids.join(",")}`);
      const kpiIds = Object.keys(cmp.kpis);
      const kpiSel = el("select", null, kpiIds.map(k => el("option", { value: k }, k)));
      const box = el("div");
      const colors = ["#4f8ff7", "#3ecf8e", "#f5b83d", "#f06a6a", "#7c5cff", "#38bdf8"];
      const draw = () => {
        box.innerHTML = "";
        const rows = cmp.kpis[kpiSel.value];
        box.append(chartLegend(rows.map((r, i) => [r.label, colors[i % colors.length]])));
        box.append(el("div", { class: "svg-wrap" }, lineChart({
          width: 880, height: 300,
          series: rows.map((r, i) => ({
            name: r.label, color: colors[i % colors.length],
            points: (r.grid || []).map((t, j) => [t, (r.series_mean || [])[j]]).filter(p => p[1] !== undefined),
          })),
        })));
        box.append(dataTable(["Сценарий", "Ожидание (итог)", "P5", "P95", "Δ к baseline", "Δ %"], rows.map(r => ({
          cells: [r.label, fmt(r.mean), fmt(r.p5), fmt(r.p95),
            r.delta_vs_baseline === null ? "—" : fmt(r.delta_vs_baseline),
            r.relative_delta === null ? "—" : (r.relative_delta * 100).toFixed(1) + "%"],
        }))));
      };
      kpiSel.addEventListener("change", draw);
      content.append(card(`Baseline: ${cmp.baseline}`, kpiSel, el("div", { style: "margin-top:10px" }, box)));
      draw();
      // парные разности: сопряжённые прогоны на общих случайных числах
      const paired = cmp.paired_differences || {};
      if (Object.keys(paired).length) {
        const pairedRows = [];
        for (const [kid, rows] of Object.entries(paired)) {
          for (const r of rows) {
            if (!r.n_pairs) continue;
            pairedRows.push({
              cells: [kid, r.label, r.n_pairs,
                r.paired_by_seed ? "по сидам" : "по порядку",
                fmt(r.mean), fmt(r.std),
                `[${fmt(r.ci_low)}; ${fmt(r.ci_high)}]`,
                r.significant ? "значимо" : "не значимо",
                (r.share_better * 100).toFixed(0) + "%"],
            });
          }
        }
        if (pairedRows.length) {
          content.append(card("Парные разности к baseline (95% ДИ)",
            el("p", { class: "muted small" },
              "Прогоны сопряжены по сиду: общая случайность вычитается, поэтому " +
              "интервал узкий. Вывод «лучше» считается обоснованным, если ноль вне интервала."),
            dataTable(["KPI", "Сценарий", "Пар", "Сопряжение", "Средняя разность",
              "СКО разности", "95% ДИ", "Вывод", "Доля прогонов лучше"], pairedRows)));
        }
      }
      if ((cmp.paired_npv || []).length) {
        content.append(card("Парные разности NPV",
          dataTable(["Сценарий", "Пар", "Средняя разность NPV", "95% ДИ", "Вывод"],
            cmp.paired_npv.map(r => ({
              cells: [r.label, r.n_pairs, fmt(r.mean),
                `[${fmt(r.ci_low)}; ${fmt(r.ci_high)}]`,
                r.significant ? "значимо" : "не значимо"],
            })))));
      }
      if ((cmp.economics || []).length) {
        const articles = [...new Set(cmp.economics.flatMap(e => Object.keys(e.by_article || {})))].sort();
        content.append(card("Экономика сценариев",
          dataTable(["Сценарий", "NPV", "Итог (номинал)", "Доход", "Расход", "Разогрев, ч"],
            cmp.economics.map(e => ({
              cells: [e.label, fmt(e.npv), fmt(e.net), fmt(e.income), fmt(e.expense),
                e.warmup_hours ?? 0],
            }))),
          articles.length
            ? dataTable(["Статья", ...cmp.economics.map(e => e.label)],
                articles.map(a => ({
                  cells: [a, ...cmp.economics.map(e => fmt((e.by_article[a] || {}).net))],
                })))
            : null));
      }
      if (Object.keys(cmp.goals).length) {
        content.append(card("Вероятность достижения целей",
          dataTable(["Цель", ...cmp.goals[Object.keys(cmp.goals)[0]].map(r => r.label)],
            Object.entries(cmp.goals).map(([gid, rows]) => ({
              cells: [gid, ...rows.map(r => r.probability === null ? "—" : (r.probability * 100).toFixed(0) + "%")],
            })))));
      }
    } catch (e) { showError(e); }
  }
}

/* -------------------------------- optimize --------------------------------- */

async function vOptimize(main) {
  if (needEnterprise(main)) return;
  main.append(el("h1", null, "Оптимизация"),
    el("p", { class: "subtitle" }, "Поиск управленческих решений по решётке значений (grid search) поверх Монте-Карло; общие random numbers для честного сравнения"));
  const [scenarios, kpis] = await Promise.all([
    API.get(`/scenarios?enterprise_id=${State.eid}`),
    API.get(`/enterprises/${State.eid}/entities/kpi`),
  ]);
  if (!scenarios.length) { main.append(card(null, el("p", { class: "muted" }, "Сначала создайте сценарий."))); return; }
  const scnSel = el("select", null, scenarios.map(s => el("option", { value: s.id }, s.name)));
  const kpiSel = el("select", null, kpis.map(k => el("option", { value: k.id }, k.name || k.id)));
  const dirSel = el("select", null, el("option", { value: "maximize" }, "максимизировать"), el("option", { value: "minimize" }, "минимизировать"));
  const varsTa = el("textarea", { style: "min-height:100px" });
  varsTa.value = JSON.stringify([{ name: "lines", path: "resource:production_line", values: [2, 3, 4] }], null, 2);
  const consTa = el("textarea", { style: "min-height:56px", placeholder: "по одному выражению на строку, напр. kpi('service_level') >= 75" });
  const runsInp = el("input", { type: "number", value: 5, min: 1, max: 50, style: "width:80px" });
  const seedInp = el("input", { type: "number", value: 7, style: "width:110px" });
  const content = el("div");
  main.append(card("Задача оптимизации",
    el("div", { class: "row" },
      el("div", null, el("label", null, "Базовый сценарий"), scnSel),
      el("div", null, el("label", null, "Целевая функция (KPI)"), kpiSel),
      el("div", null, el("label", null, "Направление"), dirSel),
      el("div", null, el("label", null, "Прогонов на оценку"), runsInp),
      el("div", null, el("label", null, "Seed"), seedInp)),
    el("label", null, "Переменные решения (пути: resource:<id> | event_rate:<id> | sed_duration:<id> | stock:<id> | param:<тип>:<id>.<поле>)"),
    varsTa,
    el("label", null, "Ограничения"),
    consTa,
    el("div", { class: "btn-row" }, el("button", { class: "primary", onclick: start }, "▶ Запустить оптимизацию"))),
    content);

  async function start() {
    let variables;
    try { variables = JSON.parse(varsTa.value); } catch (e) { toast("Невалидный JSON переменных", "err"); return; }
    const constraints = consTa.value.split("\n").map(s => s.trim()).filter(Boolean);
    try {
      const r = await API.post("/optimizations", {
        scenario_id: scnSel.value, variables, objective_kpi_id: kpiSel.value,
        direction: dirSel.value, constraints,
        runs_per_evaluation: parseInt(runsInp.value) || 5, seed: parseInt(seedInp.value) || 0,
      });
      toast("Оптимизация запущена: " + r.id, "ok");
      poll(r.id);
    } catch (e) { showError(e); }
  }

  function poll(oid) {
    content.innerHTML = "";
    const bar = el("div", { class: "progress-bar" }, el("div", { style: "width:0%" }));
    const status = el("span", { class: "badge info" }, "выполняется…");
    const box = el("div");
    content.append(card(`Оптимизация ${oid}`, status, el("div", { style: "margin:10px 0" }, bar), box));
    const timer = setInterval(async () => {
      try {
        const st = await API.get(`/optimizations/${oid}`);
        if (st.progress && st.progress.progress !== undefined)
          bar.firstChild.style.width = `${Math.round(st.progress.progress * 100)}%`;
        if (!["pending", "running", "paused"].includes(st.status)) {
          clearInterval(timer);
          bar.firstChild.style.width = "100%";
          status.textContent = st.status;
          status.className = "badge " + (st.status === "completed" ? "ok" : "err");
          if (st.result) renderResult(st.result);
        }
      } catch (e) { clearInterval(timer); showError(e); }
    }, 1000);

    function renderResult(res) {
      box.innerHTML = "";
      if (res.best) {
        box.append(el("div", { class: "kpi-row" },
          kpiTile("Лучшее значение", fmt(res.best.score), "ok"),
          ...Object.entries(res.best.assignment).map(([k, v]) => kpiTile(k, v))));
      } else {
        box.append(el("p", { class: "muted" }, "Допустимых решений не найдено (infeasible)."));
      }
      const varNames = res.evaluations.length ? Object.keys(res.evaluations[0].assignment) : [];
      box.append(dataTable(["#", ...varNames, "Значение цели", "Допустимо", "KPI (итоги)"],
        res.evaluations.map(ev => ({
          cells: [ev.index, ...varNames.map(v => ev.assignment[v]), fmt(ev.score),
            ev.feasible ? el("span", { class: "badge ok" }, "да") : el("span", { class: "badge err" }, "нет"),
            Object.entries(ev.final_kpis).slice(0, 4).map(([k, v]) => `${k}: ${fmt(v)}`).join("; ")],
        }))));
    }
  }
}

/* ---------------------------------- graph ---------------------------------- */

async function vGraph(main) {
  if (needEnterprise(main)) return;
  main.append(el("h1", null, "Граф знаний"),
    el("p", { class: "subtitle" }, "Все сущности и связи цифрового двойника; анализ влияния и зависимостей"));
  const full = await API.get(`/enterprises/${State.eid}/graph`);
  const allTypes = Object.keys(full.stats.nodes);
  let activeTypes = new Set(allTypes.filter(t => t !== "erp_object"));
  const pills = el("div", null, allTypes.map(t => {
    const pill = el("span", { class: `pill-filter ${activeTypes.has(t) ? "on" : ""}`, onclick: () => {
      if (activeTypes.has(t)) activeTypes.delete(t); else activeTypes.add(t);
      pill.classList.toggle("on");
      draw();
    } }, `${TYPE_LABELS[t] || t} (${full.stats.nodes[t]})`);
    return pill;
  }));
  const searchInp = el("input", { placeholder: "Поиск сущности…", style: "width:240px" });
  const detail = el("div", { class: "col", style: "max-width:380px" });
  const graphBox = el("div", { class: "col" });
  main.append(card(null, pills, el("div", { class: "btn-row" }, searchInp,
    el("button", { class: "small", onclick: draw }, "Перерисовать"))));
  main.append(el("div", { class: "row" }, graphBox, detail));

  let highlightNodes = null, currentView = null;

  function draw() {
    graphBox.innerHTML = "";
    const nodes = full.nodes.filter(n => activeTypes.has(n.type));
    const keep = new Set(nodes.map(n => n.id));
    const edges = full.relations.filter(r => keep.has(r.source) && keep.has(r.target));
    currentView = graphView({
      nodes, edges, height: 620,
      positions: State.nodePositions("kg"),
      highlightNodes,
      onMove: pos => State.saveNodePositions("kg", pos),
      onSelect: showNode,
    });
    graphBox.append(currentView);
  }

  async function showNode(n) {
    detail.innerHTML = "";
    try {
      const explain = await API.get(`/enterprises/${State.eid}/ai/explain/${n.id}`);
      detail.append(card(`${TYPE_LABELS[n.type] || n.type}: ${n.label}`,
        el("div", null, explain.statements.slice(0, 12).map(s => el("div", { class: "small" }, "• " + s))),
        el("div", { class: "btn-row" },
          el("button", { class: "small", onclick: async () => {
            const imp = await API.get(`/enterprises/${State.eid}/graph/impact/${n.id}`);
            highlightNodes = new Set(imp.nodes.map(x => x.id));
            draw();
            detail.append(card(`Влияние «${n.label}» (${imp.chains.length} цепочек)`,
              el("div", null, imp.chains.slice(0, 12).map(c =>
                el("div", { class: "small mono" }, c.map(id => (full.nodes.find(x => x.id === id) || { label: id }).label).join(" → "))))));
          } }, "Влияние ↓"),
          el("button", { class: "small", onclick: async () => {
            const dep = await API.get(`/enterprises/${State.eid}/graph/dependencies/${n.id}`);
            highlightNodes = new Set(dep.nodes.map(x => x.id));
            draw();
            detail.append(card(`Зависимости «${n.label}»`,
              el("div", null, dep.chains.slice(0, 12).map(c =>
                el("div", { class: "small mono" }, c.map(id => (full.nodes.find(x => x.id === id) || { label: id }).label).join(" → "))))));
          } }, "Зависимости ↑"),
          el("button", { class: "small", onclick: () => { highlightNodes = null; draw(); } }, "Сброс"))));
    } catch (e) { showError(e); }
  }

  searchInp.addEventListener("keydown", async ev => {
    if (ev.key !== "Enter") return;
    const results = await API.get(`/enterprises/${State.eid}/ai/search?q=${encodeURIComponent(searchInp.value)}`);
    if (!results.length) { toast("Ничего не найдено", "err"); return; }
    highlightNodes = new Set(results.map(r => r.id));
    draw();
    showNode(results[0]);
  });

  draw();
  const focus = sessionStorage.getItem("dtb_focus");
  if (focus) {
    sessionStorage.removeItem("dtb_focus");
    const node = full.nodes.find(n => n.id === focus);
    if (node) { highlightNodes = new Set([focus]); draw(); showNode(node); }
  }
}

/* ------------------------------------ AI ------------------------------------ */

async function vAi(main) {
  if (needEnterprise(main)) return;
  main.append(el("h1", null, "AI-ассистент"),
    el("p", { class: "subtitle" }, "Все ответы вычисляются из структурированной модели и результатов симуляций — не генерируются «из воздуха»"));
  const searchInp = el("input", { placeholder: "Найти сущность (например: производство)…", style: "width:320px" });
  const searchOut = el("div");
  searchInp.addEventListener("keydown", async ev => {
    if (ev.key !== "Enter") return;
    const results = await API.get(`/enterprises/${State.eid}/ai/search?q=${encodeURIComponent(searchInp.value)}`);
    searchOut.innerHTML = "";
    searchOut.append(dataTable(["Тип", "Название", "ID"], results.map(r => ({
      cells: [TYPE_LABELS[r.type] || r.type, r.label, el("span", { class: "mono" }, r.id)], r,
    })), row => { sessionStorage.setItem("dtb_focus", row.r.id); location.hash = "#/graph"; }));
  });
  const [suggestions, questions] = await Promise.all([
    API.get(`/enterprises/${State.eid}/ai/suggestions`),
    API.get(`/enterprises/${State.eid}/ai/questions`),
  ]);
  main.append(card("Поиск по модели", searchInp, searchOut));
  main.append(el("div", { class: "row" },
    el("div", { class: "col" }, card(`Потенциально отсутствующие связи (${suggestions.length})`,
      suggestions.length ? el("div", null, suggestions.map(s =>
        el("div", { class: "small", style: "padding:4px 0" }, el("span", { class: "badge warn" }, s.kind), " " + s.suggestion)))
        : el("p", { class: "muted" }, "Модель выглядит связной."))),
    el("div", { class: "col" }, card("Вопросы для уточнения модели",
      questions.length ? el("div", null, questions.map(q => el("div", { class: "small", style: "padding:4px 0" }, "• " + q)))
        : el("p", { class: "muted" }, "Вопросов нет.")))));
  // deviations (needs scenario with completed sim)
  const scenarios = await API.get(`/scenarios?enterprise_id=${State.eid}`);
  if (scenarios.length) {
    const sel = el("select", null, scenarios.map(s => el("option", { value: s.id }, s.name)));
    const out = el("div");
    main.append(card("Анализ причин отклонений (по прогнозу сценария)", sel,
      el("div", { class: "btn-row" }, el("button", { class: "small primary", onclick: async () => {
        out.innerHTML = "";
        try {
          const causes = await API.get(`/forecasts/deviations?scenario_id=${encodeURIComponent(sel.value)}`);
          out.append(causes.length ? el("div", null, causes.map(c =>
            el("div", { class: "small", style: "padding:3px 0" },
              el("span", { class: `badge ${c.kind === "goal_at_risk" ? "err" : "warn"}` }, c.kind), " " + c.explanation)))
            : el("p", { class: "muted" }, "Отклонений не выявлено."));
        } catch (e) { showError(e); }
      } }, "Проанализировать")), out));
  }
  main.append(el("p", { class: "small muted" },
    "Прямое изменение модели ИИ-ассистентом выполняется только через процедуру: валидация → diff → подтверждение пользователем (LLM-провайдер подключается отдельно)."));
}

/* --------------------------------- import ---------------------------------- */

async function vImport(main) {
  if (needEnterprise(main)) return;
  main.append(el("h1", null, "Импорт / Экспорт"),
    el("p", { class: "subtitle" }, "Excel-книга по видам 1–8, CSV по коллекциям, JSON-снимок модели, mock-ERP"));
  const report = el("div");

  function renderReport(rep) {
    report.innerHTML = "";
    report.append(card("Отчёт импорта",
      rep.ok ? el("span", { class: "badge ok" }, "успешно") : el("span", { class: "badge err" }, "с ошибками"),
      el("p", { class: "small", style: "margin-top:6px" },
        "Создано: " + (Object.entries(rep.created).map(([k, v]) => `${k}: ${v}`).join(", ") || "—")),
      el("p", { class: "small" },
        "Обновлено: " + (Object.entries(rep.updated).map(([k, v]) => `${k}: ${v}`).join(", ") || "—")),
      rep.errors.length ? el("div", null, el("b", { class: "small" }, "Ошибки:"),
        rep.errors.slice(0, 20).map(e2 => el("div", { class: "small", style: "color:var(--err)" },
          `${e2.where}${e2.row ? " строка " + e2.row : ""}: ${e2.message}`))) : null,
      rep.warnings.length ? el("details", null, el("summary", null, `Предупреждения (${rep.warnings.length})`),
        rep.warnings.slice(0, 20).map(w => el("div", { class: "small" }, `${w.where}: ${w.message}`))) : null));
  }

  const excelInp = el("input", { type: "file", accept: ".xlsx" });
  const jsonInp = el("input", { type: "file", accept: ".json" });
  const csvInp = el("input", { type: "file", accept: ".csv" });
  const sheetSel = el("select", null, ["goals", "seds", "utility_types", "kpis", "plans", "actuals",
    "org_units", "employees", "equipment", "resources", "event_kinds", "erp_objects", "erp_mappings",
    "initial_state"].map(s => el("option", { value: s }, s)));

  main.append(el("div", { class: "row" },
    el("div", { class: "col" },
      card("Импорт Excel (все листы)", excelInp, el("div", { class: "btn-row" },
        el("button", { class: "primary small", onclick: async () => {
          if (!excelInp.files.length) return toast("Выберите файл", "err");
          renderReport(await API.upload(`/enterprises/${State.eid}/import/excel`, excelInp.files[0]));
        } }, "Импортировать"),
        el("button", { class: "small", onclick: async () => {
          downloadBlob(await API.get("/import/template"), "dtb_template.xlsx");
        } }, "Скачать шаблон"))),
      card("Импорт CSV (одна коллекция)", sheetSel, csvInp, el("div", { class: "btn-row" },
        el("button", { class: "primary small", onclick: async () => {
          if (!csvInp.files.length) return toast("Выберите файл", "err");
          renderReport(await API.upload(`/enterprises/${State.eid}/import/csv?sheet=${sheetSel.value}`, csvInp.files[0]));
        } }, "Импортировать"))),
      card("Импорт JSON (полная модель)", jsonInp, el("div", { class: "btn-row" },
        el("button", { class: "primary small", onclick: async () => {
          if (!jsonInp.files.length) return toast("Выберите файл", "err");
          renderReport(await API.upload(`/enterprises/${State.eid}/import/json`, jsonInp.files[0]));
        } }, "Импортировать")))),
    el("div", { class: "col" },
      card("Экспорт", el("div", { class: "btn-row" },
        el("button", { onclick: async () => downloadBlob(await API.get(`/enterprises/${State.eid}/export/excel`), `${State.eid}_model.xlsx`) }, "Excel-книга"),
        el("button", { onclick: async () => downloadBlob(await API.get(`/enterprises/${State.eid}/export/json`), `${State.eid}_model.json`) }, "JSON-модель"),
        el("button", { onclick: async () => downloadBlob(await API.get(`/enterprises/${State.eid}/export/graph-json`), `${State.eid}_graph.json`) }, "Graph JSON"))),
      card("ERP-коннекторы",
        el("p", { class: "small muted" }, "Generic REST-коннектор и адаптеры 1С/Галактика подключаются на сервере (см. ERP_INTEGRATION.md). Для демонстрации доступен mock-ERP."),
        el("div", { class: "btn-row" },
          el("button", { class: "primary small", onclick: async () => {
            renderReport(await API.post(`/enterprises/${State.eid}/erp/sync-mock`));
          } }, "Синхронизировать mock-ERP"))))),
    report);
}

/* --------------------------------- versions --------------------------------- */

async function vVersions(main) {
  if (needEnterprise(main)) return;
  const versions = await API.get(`/enterprises/${State.eid}/versions`);
  main.append(el("h1", null, "Версии модели"),
    el("p", { class: "subtitle" }, "Снимки цифрового двойника: кто, что и когда изменил; сравнение версий"));
  main.append(card(null,
    el("div", { class: "btn-row" }, el("button", { class: "primary", onclick: async () => {
      const comment = prompt("Комментарий к версии:") || "";
      const r = await API.post(`/enterprises/${State.eid}/versions`, { comment });
      toast(`Сохранена версия ${r.version}`, "ok"); route();
    } }, "Сохранить текущую версию")),
    dataTable(["Версия", "Хеш", "Автор", "Комментарий", "Создана"], versions.map(v => ({
      cells: [v.version, el("span", { class: "mono" }, v.content_hash), v.author, v.comment, v.created_at],
    })))));
  if (versions.length) {
    const fromSel = el("select", null, versions.map(v => el("option", { value: v.version }, "v" + v.version)));
    const toSel = el("select", null, el("option", { value: 0 }, "текущая (рабочая)"),
      versions.map(v => el("option", { value: v.version }, "v" + v.version)));
    const out = el("div");
    main.append(card("Сравнение версий",
      el("div", { class: "row" },
        el("div", null, el("label", null, "От"), fromSel),
        el("div", null, el("label", null, "К"), toSel)),
      el("div", { class: "btn-row" }, el("button", { class: "primary small", onclick: async () => {
        out.innerHTML = "";
        const diff = await API.get(`/enterprises/${State.eid}/versions-diff?from=${fromSel.value}&to=${toSel.value}`);
        const colls = Object.entries(diff.collections);
        if (!colls.length && !diff.enterprise.length) out.append(el("p", { class: "muted" }, "Различий нет."));
        diff.enterprise.forEach(ch => out.append(el("div", { class: "small" },
          el("span", { class: "badge info" }, "enterprise"), ` ${ch.field}: ${JSON.stringify(ch.from)} → ${JSON.stringify(ch.to)}`)));
        colls.forEach(([coll, d]) => {
          out.append(el("h2", null, coll));
          d.added.forEach(id => out.append(el("div", { class: "small" }, el("span", { class: "badge ok" }, "+"), " " + id)));
          d.removed.forEach(id => out.append(el("div", { class: "small" }, el("span", { class: "badge err" }, "−"), " " + id)));
          d.changed.forEach(ch => out.append(el("div", { class: "small" },
            el("span", { class: "badge warn" }, "~"), ` ${ch.id}: `,
            ch.changes.slice(0, 6).map(f => `${f.field}: ${JSON.stringify(f.from)} → ${JSON.stringify(f.to)}`).join("; "))));
        });
      } }, "Показать diff")), out));
  }
}

/* ---------------------------------- audit ----------------------------------- */

async function vAudit(main) {
  if (needEnterprise(main)) return;
  const entries = await API.get(`/audit?enterprise_id=${State.eid}`);
  main.append(el("h1", null, "Журнал изменений (audit trail)"),
    el("p", { class: "subtitle" }, "Кто → что → когда изменил"));
  main.append(card(null, dataTable(["Время", "Актор", "Действие", "Сущность", "ID"], entries.map(a => ({
    cells: [a.ts, a.actor, el("span", { class: "badge info" }, a.action), a.entity_type, a.entity_id],
  })))));
}

const VIEWS = {
  dashboard: vDashboard, enterprise: vEnterprise, goals: vGoals, seds: vSeds,
  events: vEvents, org: vOrg, resources: vResources, erp: vErp,
  scenarios: vScenarios, simulate: vSimulate, results: vResults, forecast: vForecast,
  compare: vCompare, optimize: vOptimize, graph: vGraph, ai: vAi,
  import: vImport, versions: vVersions, audit: vAudit,
};
