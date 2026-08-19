/* DTB API client + application state. No domain logic here (rule 3): the SPA
   only calls the backend and renders results. */
"use strict";

const API = {
  key: localStorage.getItem("dtb_api_key") || "",

  headers() {
    const h = { "Content-Type": "application/json" };
    if (this.key) h["X-API-Key"] = this.key;
    return h;
  },

  async req(method, path, body, isForm) {
    const opts = { method, headers: isForm ? (this.key ? { "X-API-Key": this.key } : {}) : this.headers() };
    if (body !== undefined && !isForm) opts.body = JSON.stringify(body);
    if (isForm) opts.body = body;
    const res = await fetch("/api" + path, opts);
    if (!res.ok) {
      let detail;
      try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = res.status; err.detail = detail;
      throw err;
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("json") ? res.json() : res.blob();
  },

  get(path) { return this.req("GET", path); },
  post(path, body) { return this.req("POST", path, body); },
  put(path, body) { return this.req("PUT", path, body); },
  del(path) { return this.req("DELETE", path); },
  upload(path, file) {
    const fd = new FormData();
    fd.append("file", file);
    return this.req("POST", path, fd, true);
  },
};

const State = {
  eid: localStorage.getItem("dtb_eid") || null,
  enterprises: [],
  cache: {},

  setEnterprise(eid) {
    this.eid = eid;
    this.cache = {};
    if (eid) localStorage.setItem("dtb_eid", eid); else localStorage.removeItem("dtb_eid");
    renderEnterprisePicker();
    route();
  },

  async loadEnterprises() {
    this.enterprises = await API.get("/enterprises");
    if (this.eid && !this.enterprises.some(e => e.id === this.eid)) this.eid = null;
    if (!this.eid && this.enterprises.length) this.eid = this.enterprises[0].id;
    renderEnterprisePicker();
  },

  nodePositions(view) {
    try { return JSON.parse(localStorage.getItem(`dtb_pos_${this.eid}_${view}`)) || {}; }
    catch { return {}; }
  },
  saveNodePositions(view, pos) {
    localStorage.setItem(`dtb_pos_${this.eid}_${view}`, JSON.stringify(pos));
  },
};

/* ---------- tiny DOM helpers ---------- */

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(child));
  }
  return node;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmt(v, digits) {
  if (v === null || v === undefined) return "—";
  if (typeof v !== "number") return String(v);
  if (Number.isInteger(v) && Math.abs(v) < 1e15) return v.toLocaleString("ru-RU");
  return v.toLocaleString("ru-RU", { maximumFractionDigits: digits ?? 1 });
}

function fmtT(hours) {
  if (hours === null || hours === undefined) return "—";
  const d = Math.floor(hours / 24), h = hours % 24;
  return d > 0 ? `${d}д ${h.toFixed(1)}ч` : `${h.toFixed(1)}ч`;
}

function toast(message, kind) {
  const root = document.getElementById("toast-root");
  const node = el("div", { class: `toast ${kind || ""}` }, message);
  root.append(node);
  setTimeout(() => node.remove(), 5000);
}

function showError(e) {
  console.error(e);
  let msg = e.message || String(e);
  if (msg.length > 300) msg = msg.slice(0, 300) + "…";
  toast(msg, "err");
}

function modal(title, contentNode, actions) {
  const root = document.getElementById("modal-root");
  const closeAll = () => { root.innerHTML = ""; };
  const box = el("div", { class: "modal" }, el("h3", null, title), contentNode);
  const btnRow = el("div", { class: "btn-row" });
  (actions || []).forEach(([label, fn, cls]) => {
    btnRow.append(el("button", {
      class: cls || "",
      onclick: async () => { try { if (await fn() !== false) closeAll(); } catch (e) { showError(e); } },
    }, label));
  });
  btnRow.append(el("button", { onclick: closeAll }, "Закрыть"));
  box.append(btnRow);
  root.innerHTML = "";
  root.append(box);
  return closeAll;
}

function jsonEditorModal(title, template, onSave) {
  const ta = el("textarea", { style: "min-height:280px" });
  ta.value = JSON.stringify(template, null, 2);
  modal(title, el("div", null, ta), [
    ["Сохранить", async () => {
      let data;
      try { data = JSON.parse(ta.value); }
      catch (e) { toast("Невалидный JSON: " + e.message, "err"); return false; }
      await onSave(data);
    }, "primary"],
  ]);
}

function confirmModal(text, onYes) {
  modal("Подтверждение", el("p", null, text), [["Да", onYes, "danger"]]);
}
