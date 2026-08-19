/* Router + bootstrap. */
"use strict";

function renderEnterprisePicker() {
  const box = document.getElementById("ent-picker");
  box.innerHTML = "";
  const sel = el("select", {
    onchange: ev => State.setEnterprise(ev.target.value || null),
  });
  sel.append(el("option", { value: "" }, "— предприятие —"));
  State.enterprises.forEach(e => {
    const opt = el("option", { value: e.id }, e.name || e.id);
    if (e.id === State.eid) opt.selected = true;
    sel.append(opt);
  });
  box.append(sel);
}

async function route() {
  const main = document.getElementById("main");
  const hash = location.hash.replace(/^#\//, "") || "dashboard";
  const view = VIEWS[hash] || vDashboard;
  document.querySelectorAll("#nav a").forEach(a =>
    a.classList.toggle("active", a.dataset.view === hash));
  main.innerHTML = "";
  main.append(el("div", { class: "loading" }, "Загрузка…"));
  try {
    main.innerHTML = "";
    await view(main);
  } catch (e) {
    main.innerHTML = "";
    main.append(card("Ошибка", el("p", null, e.message || String(e))));
    console.error(e);
  }
}

async function boot() {
  window.addEventListener("hashchange", route);
  try {
    const health = await API.get("/health");
    document.getElementById("health-dot").className = "dot ok";
    document.getElementById("health-text").textContent =
      `v${health.version} · ${health.auth === "enabled" ? "авторизация" : "dev-режим"}`;
    if (health.auth === "enabled" && !API.key) {
      const key = prompt("Введите API-ключ (X-API-Key):") || "";
      API.key = key;
      localStorage.setItem("dtb_api_key", key);
    }
  } catch (e) {
    document.getElementById("health-dot").className = "dot err";
    document.getElementById("health-text").textContent = "API недоступен";
  }
  try { await State.loadEnterprises(); } catch (e) { console.error(e); }
  route();
}

boot();
