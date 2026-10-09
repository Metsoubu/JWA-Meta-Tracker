/* JWA Meta Tracker dashboard. Plain JavaScript, no external libraries.
 *
 * Everything shown comes from data/site.json: for every snapshot and rank range it
 * holds the number of valid teams and how many use each creature (counted in
 * Python). This file only adds those counts together, divides, and draws.
 * The same page works on your own computer and as a static website.
 */
"use strict";
(function () {
  // ------------------------------------------------------------------ constants
  const MAX_SERIES = 6;
  const TREND_POINTS = 12;
  const SUCCESS = ["success", "no_new_data", "partial"];
  const HYBRID_LABEL = {
    non_hybrid: "Non-hybrid", hybrid: "Hybrid", super_hybrid: "Super hybrid",
    mega_hybrid: "Mega hybrid", giga_hybrid: "Giga hybrid",
  };
  const CLASS_LABEL = {
    fierce: "Fierce", cunning: "Cunning", resilient: "Resilient", fierce_resilient: "Fierce Resilient",
    cunning_fierce: "Cunning Fierce", cunning_resilient: "Cunning Resilient", wild_card: "Wild Card",
  };
  const PERIODS = [["30", "Last 30 days"], ["90", "Last 90 days"], ["all", "All time"]];

  // ------------------------------------------------------------------ state
  const DEFAULT_STATE = {
    range: "top100", kind: "arena", sel: "latest", sort: "usage-desc", view: "tiers",
    tiers: ["S", "A", "B", "C", "D"], hBase: "", hCurrent: "latest", chartKeys: null, period: "30",
  };
  const state = Object.assign({}, DEFAULT_STATE, loadSaved());
  let SITE = null;          // data/site.json
  let BY_KIND = {};         // kind -> snapshots, newest first
  let TIERS = ["S", "A", "B", "C", "D"];
  let TIER_LABEL = {};
  let LOCAL = null;         // local server status (null on the website)
  let TIER_DATA = null;
  let HISTORY = null;
  const colorSlots = {};    // creature key -> chart colour slot (colour follows the creature)

  function loadSaved() {
    try {
      const p = JSON.parse(localStorage.getItem("jwa-state") || "{}");
      const out = {
        range: typeof p.range === "string" ? p.range : undefined,
        kind: p.kind === "tournament" ? "tournament" : undefined,
        sort: ["usage-desc", "usage-asc", "name", "rise", "fall"].includes(p.sort) ? p.sort : undefined,
        view: p.view === "table" ? "table" : undefined,
        tiers: Array.isArray(p.tiers) && p.tiers.length ? p.tiers : undefined,
        chartKeys: Array.isArray(p.chartKeys) && p.chartKeys.length ? p.chartKeys.slice(0, MAX_SERIES) : undefined,
        period: ["30", "90", "all"].includes(p.period) ? p.period : undefined,
      };
      Object.keys(out).forEach(k => { if (out[k] === undefined) delete out[k]; });
      return out;
    } catch (e) {
      return {};
    }
  }
  function save() {
    try {
      localStorage.setItem("jwa-state", JSON.stringify({
        range: state.range, kind: state.kind, sort: state.sort, view: state.view,
        tiers: state.tiers, chartKeys: state.chartKeys, period: state.period,
      }));
    } catch (e) { /* storage unavailable */ }
  }

  // ------------------------------------------------------------------ dom helpers
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "style") for (const [sk, sv] of Object.entries(v)) node.style.setProperty(sk, sv);
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return node;
  }
  const SVGNS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs, ...children) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== undefined && v !== null) node.setAttribute(k, String(v));
    for (const c of children.flat()) if (c) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    return node;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }
  const ICONS = {
    warn: "M12 3 2 20h20L12 3Zm0 6v5m0 3v.5",
    error: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 5v5m0 3v.5",
    info: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 7v6m0-9v.5",
    check: "M5 12.5 10 17.5 19 7",
    x: "M6 6l12 12M18 6 6 18",
    dash: "M6 12h12",
    clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4v5l3 2",
  };
  function icon(name) {
    return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, svg("path", { d: ICONS[name] }));
  }
  function banner(kind, title, text, extra) {
    return el("div", { class: `banner ${kind}` },
      el("span", { class: "b-icon" }, icon(kind === "error" ? "error" : kind === "info" ? "info" : "warn")),
      el("div", null, el("span", { class: "b-title", text: title }), text ? " " : null,
        text ? el("span", { class: "b-text", text }) : null, extra || null));
  }

  // ------------------------------------------------------------------ formatting
  function fmtNum(v) {
    const r = Math.round(v * 100) / 100;
    const oneDecimal = Math.abs(r * 10 - Math.round(r * 10)) < 1e-9;
    return r.toFixed(oneDecimal ? 1 : 2);
  }
  function fmtPct(p) { return p === null || p === undefined ? "—" : fmtNum(p) + "%"; }
  function exactPct(count, total) { return total ? (count * 100 / total).toFixed(4).replace(/\.?0+$/, "") + "%" : "—"; }
  function deltaNode(d, naText) {
    if (d === null || d === undefined) return el("span", { class: "delta na", text: naText || "—" });
    if (Math.abs(d) < 0.005) return el("span", { class: "delta flat", text: "±0.0 pp" });
    const up = d > 0;
    return el("span", { class: `delta ${up ? "up" : "down"}`, title: `${up ? "Up" : "Down"} ${fmtNum(Math.abs(d))} percentage points` },
      el("span", { class: "tri", "aria-hidden": "true" }), (up ? "+" : "−") + fmtNum(Math.abs(d)) + " pp");
  }
  const dateFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const dateLongFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
  function fmtDate(iso) { return iso ? dateFmt.format(new Date(iso)) : "—"; }
  function fmtDateLong(iso) { return iso ? dateLongFmt.format(new Date(iso)) : "—"; }
  function relTime(iso) {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return "just now";
    const m = s / 60, h = m / 60, d = h / 24;
    if (m < 60) return `${Math.max(1, Math.round(m))} min ago`;
    if (h < 36) return `${Math.round(h)} h ago`;
    return `${Math.round(d)} days ago`;
  }
  function norm(s) { return (s || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase(); }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ""; }
  function initials(name) {
    const words = (name || "?").replace(/[^A-Za-z0-9 ]+/g, " ").split(/\s+/).filter(Boolean);
    if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
    const w = words[0] || "?";
    return w.charAt(0).toUpperCase() + w.charAt(1).toLowerCase();
  }
  function shortLeaderboard(name) {
    return (name || "").replace(/^\[(Seasonal|Tournament)\]\s*/, "").replace(/\s+Seasonal League$/, " season");
  }

  // ================================================================== DATA (site.json)
  async function loadSite() {
    const resp = await fetch("data/site.json", { cache: "no-store" });
    if (!resp.ok) throw new Error(`Could not load data (HTTP ${resp.status})`);
    const data = await resp.json();
    SITE = data;
    TIERS = data.tiers.map(t => t[0]);
    TIER_LABEL = data.tier_labels;
    data.snapshots.sort((a, b) => (a.t < b.t ? -1 : a.t > b.t ? 1 : a.id < b.id ? -1 : 1));
    BY_KIND = {};
    for (const s of data.snapshots.slice().reverse()) (BY_KIND[s.k] = BY_KIND[s.k] || []).push(s);
    return data;
  }
  const creature = key => (SITE.creatures[key] || { name: key, rarity: null, listed: false });
  const snapsOf = kind => BY_KIND[kind] || [];
  const isWebsite = () => SITE && SITE.mode === "website";

  function availableRanges(kind) {
    const latest = snapsOf(kind)[0];
    if (!latest) return SITE.ranges.filter(r => r.key === "top50" || r.key === "top100" || r.key === "r51-100");
    return SITE.ranges.filter(r => latest.u[r.key]);
  }
  function ensureRange(kind) {
    const keys = availableRanges(kind).map(r => r.key);
    if (!keys.includes(state.range)) state.range = keys.includes("top100") ? "top100" : keys[keys.length - 1] || "top100";
  }
  function rangeInfo(key) { return SITE.ranges.find(r => r.key === key) || { key, label: key, min: 1, max: 100 }; }

  function resolveSel(kind, spec) {
    const snaps = snapsOf(kind);
    if (!snaps.length) return { mode: "none", snaps: [] };
    spec = spec || "latest";
    if (spec.startsWith("ver:")) {
      const version = spec.slice(4);
      const chosen = snaps.filter(s => s.v === version);
      if (chosen.length) return { mode: "version", version, snaps: chosen, spec };
    } else if (spec !== "latest") {
      const id = spec.startsWith("snap:") ? spec.slice(5) : spec;
      const s = snaps.find(x => x.id === id);
      if (s) return { mode: "snapshot", snaps: [s], spec: `snap:${s.id}`, latest: s === snaps[0] };
    }
    return { mode: "snapshot", snaps: [snaps[0]], spec: `snap:${snaps[0].id}`, latest: true };
  }
  function pool(snaps, range) {
    let total = 0, invalid = 0, missing = 0;
    const counts = {};
    for (const s of snaps) {
      invalid += s.inv || 0;
      const u = s.u[range];
      if (!u) { missing++; continue; }
      total += u.n;
      for (const k in u.c) counts[k] = (counts[k] || 0) + u.c[k];
    }
    return { total, counts, invalid, missing };
  }
  function tierFor(count, total) {
    if (total <= 0) return "D";
    for (const [t, min] of SITE.tiers) if (count * 100 >= min * total) return t; // exact: 79.99% never rounds up
    return "D";
  }
  const pct = (c, t) => (t ? (c * 100) / t : 0);
  function previousSel(kind, sel) {
    const snaps = snapsOf(kind);
    if (sel.mode === "snapshot") {
      const i = snaps.indexOf(sel.snaps[0]);
      return i >= 0 && i + 1 < snaps.length ? { mode: "snapshot", snaps: [snaps[i + 1]], spec: `snap:${snaps[i + 1].id}` } : null;
    }
    if (sel.mode === "version") {
      const order = SITE.versions.map(v => v.version);
      const present = new Set(snaps.map(s => s.v));
      for (let j = order.indexOf(sel.version) - 1; j >= 0; j--) if (present.has(order[j])) return resolveSel(kind, `ver:${order[j]}`);
    }
    return null;
  }
  function trendWindow(kind, sel) {
    if (sel.mode === "version") return sel.snaps.slice(0, TREND_POINTS).reverse();
    const snaps = snapsOf(kind);
    const i = snaps.indexOf(sel.snaps[0]);
    return snaps.slice(i, i + TREND_POINTS).reverse();
  }
  function seriesFor(snaps, range, key) {
    return snaps.map(s => { const u = s.u[range]; return u && u.n ? pct(u.c[key] || 0, u.n) : null; });
  }

  function computeTierlist(kind, range, spec) {
    const sel = resolveSel(kind, spec);
    if (sel.mode === "none") return { sel, rows: [], sample: { teams: 0, invalid: 0 }, previous: null };
    const cur = pool(sel.snaps, range);
    const prevSel = previousSel(kind, sel);
    const prev = prevSel ? pool(prevSel.snaps, range) : null;
    const trendSnaps = trendWindow(kind, sel);
    const rows = Object.keys(cur.counts).filter(k => cur.counts[k] > 0).map(key => {
      const c = creature(key);
      const count = cur.counts[key];
      const p = pct(count, cur.total);
      const prevPct = prev && prev.total ? pct(prev.counts[key] || 0, prev.total) : null;
      return {
        key, name: c.name, rarity: c.rarity, class: c.class, hybrid: c.hybrid, listed: c.listed !== false,
        img: c.img, imgKind: c.imgKind, credit: c.credit,
        count, total: cur.total, pct: p, tier: tierFor(count, cur.total),
        prev_pct: prevPct, delta_pp: prevPct === null ? null : p - prevPct,
        trend: seriesFor(trendSnaps, range, key),
      };
    });
    rows.sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
    return {
      sel, rows,
      sample: { teams: cur.total, invalid: cur.invalid, missing: cur.missing },
      previous: prevSel && prev && prev.total ? { sel: prevSel, teams: prev.total } : null,
      trendCount: trendSnaps.length,
    };
  }

  function computeCompare(kind, range, baseSpec, curSpec) {
    const bSel = resolveSel(kind, baseSpec), cSel = resolveSel(kind, curSpec);
    const b = pool(bSel.snaps, range), c = pool(cSel.snaps, range);
    const keys = new Set([...Object.keys(b.counts), ...Object.keys(c.counts)]);
    const rows = [...keys].filter(k => (b.counts[k] || 0) + (c.counts[k] || 0) > 0).map(key => {
      const info = creature(key);
      const bp = pct(b.counts[key] || 0, b.total), cp = pct(c.counts[key] || 0, c.total);
      return {
        key, name: info.name, rarity: info.rarity, img: info.img, imgKind: info.imgKind, hybrid: info.hybrid,
        base_count: b.counts[key] || 0, base_pct: bp, base_tier: tierFor(b.counts[key] || 0, b.total),
        count: c.counts[key] || 0, pct: cp, tier: tierFor(c.counts[key] || 0, c.total),
        delta_pp: b.total && c.total ? cp - bp : null,
      };
    });
    return { bSel, cSel, base: b, cur: c, rows };
  }

  function periodSnaps(kind) {
    const all = snapsOf(kind).slice().reverse(); // oldest first
    if (state.period === "all" || !all.length) return all;
    const cutoff = new Date(all[all.length - 1].t).getTime() - Number(state.period) * 86400e3;
    return all.filter(s => new Date(s.t).getTime() >= cutoff);
  }

  // ------------------------------------------------------------------ shared widgets
  function emblem(c, size) {
    const rarity = (c.rarity || "unknown").toLowerCase();
    const node = el("div", { class: `emb r-${rarity}${size ? " " + size : ""}`, "aria-hidden": "true" });
    if (c.img && c.imgKind === "custom") {
      node.appendChild(el("img", { src: c.img, alt: "", loading: "lazy" }));
    } else if (c.img) {
      const url = c.img.replace(/["\\]/g, "\\$&");
      node.appendChild(el("span", { class: "sil", style: { "--mask": `url("${url}")` } }));
    } else {
      node.appendChild(el("span", { class: "mono", text: initials(c.name) }));
    }
    return node;
  }
  function tierBadge(t) {
    return el("span", { class: "tier-badge", style: { "--tc": `var(--tier-${t})` }, title: `${t} tier (${TIER_LABEL[t]})`, text: t });
  }
  function rarityText(r) { return el("span", { class: `rar rar-${(r || "unknown").toLowerCase()}`, text: cap(r || "unknown") }); }
  function sparkline(values, w, h) {
    w = w || 84; h = h || 24;
    const node = svg("svg", { class: "spark", width: w, height: h, viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
    const pts = values.map((v, i) => [i, v]).filter(([, v]) => v !== null && v !== undefined);
    if (pts.length < 2) return node;
    const max = Math.max(10, ...pts.map(p => p[1]));
    const x = i => 2 + (i / Math.max(1, values.length - 1)) * (w - 6);
    const y = v => h - 3 - (v / max) * (h - 6);
    let d = "", pen = false;
    values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1);
      pen = true;
    });
    node.appendChild(svg("path", { class: "l", d }));
    const last = pts[pts.length - 1];
    node.appendChild(svg("circle", { cx: x(last[0]), cy: y(last[1]), r: 2.6 }));
    return node;
  }

  const tooltip = $("#tooltip");
  function showTip(x, y, content) {
    clear(tooltip).appendChild(content);
    tooltip.hidden = false;
    const r = tooltip.getBoundingClientRect();
    let left = x + 14, top = y + 14;
    if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
    if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
    tooltip.style.left = Math.max(8, left) + "px";
    tooltip.style.top = Math.max(8, top) + "px";
  }
  function hideTip() { tooltip.hidden = true; }

  let toastTimer = null;
  function toast(text) {
    const t = $("#toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
  }

  function rangeSeg(container, onChange) {
    clear(container);
    availableRanges(state.kind).forEach(r => {
      const b = el("button", { type: "button", role: "radio", "aria-checked": String(state.range === r.key),
        title: r.group === "top" ? `The ${r.max} highest-ranked players` : `Players ranked ${r.min}–${r.max}` }, r.label);
      b.addEventListener("click", () => { state.range = r.key; save(); onChange(); });
      container.appendChild(b);
    });
  }

  function snapshotOptions(select, value, opts) {
    clear(select);
    const snaps = snapsOf(state.kind);
    if (!snaps.length) {
      select.appendChild(el("option", { value: "latest", text: "No data collected yet" }));
      select.disabled = true;
      return;
    }
    select.disabled = false;
    if (opts && opts.blank) select.appendChild(el("option", { value: "", text: opts.blank }));
    const g1 = el("optgroup", { label: "Single snapshot" });
    g1.appendChild(el("option", { value: "latest", text: `Latest — ${fmtDate(snaps[0].t)} (${snaps[0].v || "version unknown"})` }));
    snaps.forEach(s => g1.appendChild(el("option", {
      value: `snap:${s.id}`, text: `${fmtDateLong(s.t)} · ${s.v || "?"}${s.ex ? " · EXAMPLE" : ""}`,
    })));
    select.appendChild(g1);
    const versions = [];
    for (const v of SITE.versions.slice().reverse()) {
      const n = snaps.filter(s => s.v === v.version).length;
      if (n) versions.push([v.version, n]);
    }
    if (versions.length) {
      const g2 = el("optgroup", { label: "Whole game version (all its snapshots combined)" });
      versions.forEach(([v, n]) => g2.appendChild(el("option", { value: `ver:${v}`, text: `${v} — ${n} snapshot${n === 1 ? "" : "s"} combined` })));
      select.appendChild(g2);
    }
    select.value = value;
    if (select.value !== value) select.value = opts && opts.blank !== undefined ? "" : "latest";
  }

  // ------------------------------------------------------------------ status
  function lastSuccessRun() { return (SITE.runs || []).find(r => SUCCESS.includes(r.status)) || null; }
  function consecutiveFailures() {
    let n = 0;
    for (const r of SITE.runs || []) { if (SUCCESS.includes(r.status)) break; n++; }
    return n;
  }
  function freshness() {
    const latest = snapsOf("arena")[0];
    if (!latest) return { state: "none", hours: null };
    const hours = (Date.now() - new Date(latest.t).getTime()) / 3600e3;
    return { state: hours > SITE.stale_after_hours ? "stale" : "fresh", hours, latest };
  }
  function renderStatusPill() {
    const pill = $("#statusPill");
    const fr = freshness();
    const fails = consecutiveFailures();
    let st = "ok", text;
    if (SITE.demo) { st = "warn"; text = "Example data (not real)"; }
    else if (LOCAL && LOCAL.collection_running) { st = "busy"; text = "Updating…"; }
    else if (fr.state === "none") { st = "warn"; text = fails ? "Update failed" : "No data yet"; }
    else if (fails) { st = "error"; text = "Last update failed"; }
    else if (fr.state === "stale") { st = "warn"; text = `Data ${relTime(fr.latest.t).replace(" ago", " old")}`; }
    else { const ok = lastSuccessRun(); text = ok ? `Updated ${relTime(ok.finished_at || ok.started_at)}` : `Data from ${relTime(fr.latest.t)}`; }
    pill.dataset.state = st;
    $(".status-text", pill).textContent = text;
  }

  async function refreshLocalStatus() {
    if (!LOCAL) return false;
    try {
      const resp = await fetch("api/status", { cache: "no-store" });
      const s = await resp.json();
      const changed = s.latest_snapshot_id !== LOCAL.latest_snapshot_id
        || JSON.stringify(s.last_run) !== JSON.stringify(LOCAL.last_run);
      const newData = s.latest_snapshot_id !== LOCAL.latest_snapshot_id;
      LOCAL = s;
      if (changed) {
        await loadSite();
        if (newData) toast("New leaderboard data has arrived.");
        reloadCurrentView();
      }
      renderStatusPill();
      return changed;
    } catch (e) {
      const pill = $("#statusPill");
      pill.dataset.state = "error";
      $(".status-text", pill).textContent = "Dashboard window was closed";
      return false;
    }
  }

  // ------------------------------------------------------------------ routing
  const VIEWS = ["tiers", "history"];
  function currentView() {
    const v = (location.hash || "").replace(/^#\/?/, "");
    return VIEWS.includes(v) ? v : "tiers";
  }
  function showView() {
    const v = currentView();
    VIEWS.forEach(name => { $(`#view-${name}`).hidden = name !== v; });
    $$(".tabs a").forEach(a => { if (a.dataset.tab === v) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    reloadCurrentView();
  }
  function reloadCurrentView() {
    hideTip();
    ensureRange(state.kind);
    const v = currentView();
    if (v === "history") renderHistory();
    else renderTiers();
  }

  // ================================================================== TIER LIST
  function renderTiers() {
    TIER_DATA = computeTierlist(state.kind, state.range, state.sel);
    if (TIER_DATA.sel.mode !== "none") state.sel = TIER_DATA.sel.latest ? "latest" : TIER_DATA.sel.spec;
    snapshotOptions($("#selData"), state.sel);
    $("#selKind").value = state.kind;
    rangeSeg($("#rangeSeg"), renderTiers);
    renderStats();
    renderTierBanners();
    renderTierContent();
  }

  function renderStats() {
    const row = clear($("#statRow"));
    const d = TIER_DATA, sel = d.sel, r = rangeInfo(state.range);
    const tile = (label, value, sub, title) => el("div", { class: "stat", title: title || null },
      el("div", { class: "stat-label", text: label }), el("div", { class: "stat-value", text: value }),
      sub ? el("div", { class: "stat-sub", text: sub }) : null);
    if (sel.mode === "none") {
      row.appendChild(tile("Teams in sample", "0", "No data yet"));
    } else if (sel.mode === "version") {
      row.appendChild(tile("Team appearances", d.sample.teams.toLocaleString(), `${r.label}, ${sel.snaps.length} snapshots`));
      row.appendChild(tile("Period", `${dayFmt.format(new Date(sel.snaps[sel.snaps.length - 1].t))} – ${dayFmt.format(new Date(sel.snaps[0].t))}`,
        `${sel.version} · all snapshots combined`));
    } else {
      const s = sel.snaps[0];
      row.appendChild(tile("Teams in sample", d.sample.teams.toLocaleString(), r.group === "top" ? `the ${r.label.toLowerCase()} players` : `ranks ${r.min}–${r.max}`));
      row.appendChild(tile("Snapshot captured", fmtDate(s.t), `${relTime(s.t)} · ${shortLeaderboard(s.lb)}`, s.lb));
    }
    const ok = lastSuccessRun();
    row.appendChild(tile("Last successful update", ok ? fmtDate(ok.finished_at || ok.started_at) : "—",
      ok ? relTime(ok.finished_at || ok.started_at) : ""));
    row.appendChild(tile("Game version", (sel.mode === "version" ? sel.version : sel.snaps && sel.snaps[0] && sel.snaps[0].v) || "—",
      SITE.app_store_build ? `latest build ${SITE.app_store_build}` : ""));
    row.appendChild(tile("Creatures used", d.rows.length.toLocaleString(), "on at least one team"));
  }

  function renderTierBanners() {
    const box = clear($("#tierBanners"));
    const d = TIER_DATA, sel = d.sel;
    if (sel.snaps && sel.snaps.some(s => s.ex)) box.appendChild(banner("example", "Example data.", "These numbers are randomly generated for testing and are not real Jurassic World Alive results."));
    if (sel.mode === "none") {
      if (LOCAL && LOCAL.collection_running) box.appendChild(banner("info", "Collecting data for the first time…", "This usually takes about a minute. The page updates by itself."));
      else box.appendChild(banner("warn", "No data yet.", isWebsite() ? "The first update has not finished yet." : "Updates run automatically; to fetch data right away, double-click UPDATE_NOW.bat."));
      return;
    }
    const fails = consecutiveFailures();
    if (fails) {
      const r = SITE.runs[0];
      box.appendChild(banner("error", "The last update failed.", `${(r.error || r.message || "").split("\n")[0]} The data shown is from the last successful update.`));
    }
    if (sel.mode === "snapshot" && !sel.latest) {
      box.appendChild(banner("info", "You are viewing a past snapshot.", `Captured ${fmtDateLong(sel.snaps[0].t)}.`,
        el("button", { class: "btn btn-ghost", type: "button", style: { "margin-left": "8px", padding: "2px 10px" }, onclick: () => { state.sel = "latest"; renderTiers(); } }, "Back to latest")));
    }
    if (sel.mode === "version") {
      box.appendChild(banner("info", `Combined view of game version ${sel.version}.`,
        `Usage is pooled over ${sel.snaps.length} snapshots; a team that appears in several snapshots is counted each time.`));
    }
    const fr = freshness();
    if (sel.latest && state.kind === "arena" && fr.state === "stale") {
      box.appendChild(banner("warn", "This data may be out of date.", `The newest snapshot was captured ${relTime(fr.latest.t)}. The data source normally publishes twice a day.`));
    }
    if (d.sample.missing) box.appendChild(banner("info", `${d.sample.missing} snapshot(s) do not cover this rank range and are left out.`, ""));
    if (d.sample.invalid) box.appendChild(banner("info", `${d.sample.invalid} incomplete team record(s) excluded.`, "Teams without exactly eight identified creatures are not counted."));
    const unmatched = d.rows.filter(c => !c.listed);
    if (unmatched.length) box.appendChild(banner("info", "Some creatures could not be identified.",
      `${unmatched.map(c => c.name).join(", ")} — shown with their raw label until the creature list catches up.`));
  }

  function filteredRows() {
    const q = norm($("#search").value.trim());
    let rows = TIER_DATA.rows.filter(c => state.tiers.includes(c.tier));
    if (q) rows = rows.filter(c => norm(c.name).includes(q) || norm(c.key).includes(q));
    const by = {
      "usage-desc": (a, b) => b.count - a.count || a.name.localeCompare(b.name),
      "usage-asc": (a, b) => a.count - b.count || a.name.localeCompare(b.name),
      name: (a, b) => a.name.localeCompare(b.name),
      rise: (a, b) => (b.delta_pp ?? -1e9) - (a.delta_pp ?? -1e9) || b.count - a.count,
      fall: (a, b) => (a.delta_pp ?? 1e9) - (b.delta_pp ?? 1e9) || b.count - a.count,
    }[state.sort] || ((a, b) => b.count - a.count);
    return rows.slice().sort(by);
  }

  function renderTierContent() {
    const content = clear($("#tierContent"));
    renderTierChips();
    $$("#viewToggle button").forEach(b => b.setAttribute("aria-checked", String(b.dataset.view === state.view)));
    $("#selSort").value = state.sort;
    if (TIER_DATA.sel.mode === "none" || !TIER_DATA.sample.teams) {
      content.appendChild(el("div", { class: "card empty-state", text: "Your tier list will appear here as soon as the first update finishes." }));
      return;
    }
    const rows = filteredRows();
    if (!rows.length) {
      content.appendChild(el("div", { class: "card empty-state", text: "No creatures match your search or tier filter." }));
      return;
    }
    if (state.view === "table") return renderRanking(content, rows);
    const searching = $("#search").value.trim() !== "";
    for (const t of TIERS) {
      if (!state.tiers.includes(t)) continue;
      const inTier = rows.filter(c => c.tier === t);
      if (searching && !inTier.length) continue;
      const body = el("div", { class: "tier-body" });
      if (inTier.length) body.appendChild(el("div", { class: "tier-grid" }, inTier.map(creatureCard)));
      else body.appendChild(el("div", { class: "tier-empty", text: `No creature is in ${t} tier right now.` }));
      content.appendChild(el("section", { class: "tier", style: { "--tc": `var(--tier-${t})` }, "aria-label": `${t} tier` },
        el("div", { class: "tier-tile" }, el("div", { class: "tier-tile-inner" },
          el("div", { class: "tier-letter", text: t }),
          el("div", { class: "tier-range", text: TIER_LABEL[t] }),
          el("div", { class: "tier-count", text: `${inTier.length} creature${inTier.length === 1 ? "" : "s"}` }))),
        body));
    }
  }

  function creatureCard(c) {
    const card = el("button", { class: "cc", type: "button", "aria-label": `${c.name}: ${fmtPct(c.pct)} usage, ${c.tier} tier. Open details.` },
      emblem(c),
      el("div", { class: "cc-main" },
        el("div", { class: "cc-name", title: c.name }, c.name, !c.listed ? el("span", { class: "badge-unmatched", text: "unverified name" }) : null),
        el("div", { class: "cc-sub" }, rarityText(c.rarity), ` · ${c.count.toLocaleString()}/${c.total.toLocaleString()} teams`),
        el("div", { class: "bar" }, el("span", { style: { width: Math.min(100, c.pct).toFixed(2) + "%" } }))),
      el("div", { class: "cc-side" }, el("div", { class: "cc-pct", text: fmtPct(c.pct) }), deltaNode(c.delta_pp)));
    card.addEventListener("click", () => openDrawer(c.key));
    card.addEventListener("pointermove", ev => showTip(ev.clientX, ev.clientY, creatureTip(c)));
    card.addEventListener("pointerleave", hideTip);
    return card;
  }
  function creatureTip(c) {
    const prev = TIER_DATA.previous;
    return el("div", null,
      el("div", { class: "tt-title", text: `${c.name} · ${c.tier} tier` }),
      el("div", null, el("strong", { text: exactPct(c.count, c.total) }), ` — ${c.count} of ${c.total} teams`),
      c.prev_pct !== null && prev
        ? el("div", { class: "tt-title", text: `Previously ${fmtPct(c.prev_pct)} (${prev.sel.mode === "version" ? prev.sel.version : fmtDate(prev.sel.snaps[0].t)})` })
        : null);
  }

  function renderRanking(content, rows) {
    const counts = TIER_DATA.rows.map(c => c.count).sort((a, b) => b - a);
    const rankOf = {};
    counts.forEach((c, i) => { if (rankOf[c] === undefined) rankOf[c] = i + 1; });
    const tbody = el("tbody");
    rows.forEach(c => {
      const tr = el("tr", { tabindex: "0" },
        el("td", { class: "num", text: rankOf[c.count] }),
        el("td", null, el("div", { class: "cell-creature" }, emblem(c, "sm"),
          el("div", null, el("div", { class: "n", text: c.name }), el("div", { class: "s" }, rarityText(c.rarity),
            c.class ? ` · ${CLASS_LABEL[c.class] || c.class}` : "")))),
        el("td", null, tierBadge(c.tier)),
        el("td", null, el("div", { class: "cell-bar" },
          el("div", { class: "bar" }, el("span", { style: { width: Math.min(100, c.pct).toFixed(2) + "%" } })),
          el("span", { class: "v", text: fmtPct(c.pct) }))),
        el("td", { class: "num", text: `${c.count} / ${c.total}` }),
        el("td", { class: "num" }, deltaNode(c.delta_pp)),
        el("td", null, sparkline(c.trend || [])));
      const open = () => openDrawer(c.key);
      tr.addEventListener("click", open);
      tr.addEventListener("keydown", ev => { if (ev.key === "Enter") open(); });
      tbody.appendChild(tr);
    });
    content.appendChild(el("div", { class: "table-wrap" },
      el("table", { class: "data" },
        el("thead", null, el("tr", null,
          el("th", { class: "num", text: "#" }), el("th", { text: "Creature" }), el("th", { text: "Tier" }),
          el("th", { text: "Usage" }), el("th", { class: "num", text: "Teams" }), el("th", { class: "num", text: "Change" }),
          el("th", { text: `Trend (last ${TIER_DATA.trendCount} snapshots)` }))),
        tbody)));
  }

  function renderTierChips() {
    const box = clear($("#tierChips"));
    const all = el("button", { type: "button", "aria-pressed": String(state.tiers.length === TIERS.length) }, "All");
    all.addEventListener("click", () => { state.tiers = TIERS.slice(); save(); renderTierContent(); });
    box.appendChild(all);
    TIERS.forEach(t => {
      const on = state.tiers.includes(t) && state.tiers.length !== TIERS.length;
      const b = el("button", { type: "button", "aria-pressed": String(on), title: `Show only ${t} tier (${TIER_LABEL[t]})` },
        el("span", { class: "tl", style: { background: `var(--tier-${t})` } }), t);
      b.addEventListener("click", () => {
        if (state.tiers.length === TIERS.length) state.tiers = [t];
        else if (state.tiers.includes(t)) state.tiers = state.tiers.filter(x => x !== t);
        else state.tiers = state.tiers.concat(t);
        if (!state.tiers.length) state.tiers = TIERS.slice();
        save();
        renderTierContent();
      });
      box.appendChild(b);
    });
  }

  // ================================================================== DRAWER
  let lastFocus = null;
  function openDrawer(key) {
    hideTip();
    const drawer = $("#drawer");
    lastFocus = document.activeElement;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    $(".drawer-close", drawer).focus();
    renderDrawer(key);
  }
  function closeDrawer() {
    const drawer = $("#drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  function renderDrawer(key) {
    const body = clear($("#drawerBody"));
    const c = Object.assign({ key }, creature(key));
    const sel = resolveSel(state.kind, state.sel);
    const meta = [cap(c.rarity || "unknown"), CLASS_LABEL[c.class] || null, HYBRID_LABEL[c.hybrid] || null,
      c.added ? `added in ${c.added}` : null].filter(Boolean).join(" · ");
    body.appendChild(el("div", { class: "d-head" }, emblem(c, "lg"),
      el("div", null, el("h2", { id: "drawerTitle", text: c.name }), el("div", { class: "d-meta", text: meta }),
        c.listed === false ? el("div", { class: "badge-unmatched", text: "Name not found in the creature list — shown as the data source labels it." }) : null)));
    const ranges = availableRanges(state.kind);
    const brackets = ranges.map(r => {
      const p = pool(sel.snaps, r.key);
      return { r, count: p.counts[key] || 0, total: p.total };
    });
    const cur = brackets.find(b => b.r.key === state.range);
    if (cur) {
      body.appendChild(el("div", { class: "d-hero" },
        el("div", { class: "hv", text: cur.total ? fmtPct(pct(cur.count, cur.total)) : "—" }),
        el("div", { class: "hs" }, cur.total ? tierBadge(tierFor(cur.count, cur.total)) : null, " ",
          cur.total ? `${cur.count} of ${cur.total} teams · ${cur.r.label}` : "No teams")));
    }
    const sec = (title, ...kids) => el("div", { class: "d-section" }, el("h3", { text: title }), ...kids);
    body.appendChild(sec("Usage by rank range",
      el("div", { class: "d-brackets" }, brackets.map(b => el("div", { class: "d-brk" },
        el("span", { class: "lbl", text: b.r.label }),
        el("div", { class: "bar" }, el("span", { style: { width: (b.total ? Math.min(100, pct(b.count, b.total)) : 0).toFixed(2) + "%" } })),
        el("span", { class: "v", text: b.total ? fmtPct(pct(b.count, b.total)) : "—" }))))));
    const points = periodSnaps(state.kind);
    const chartBox = el("div", { class: "chart-wrap" });
    body.appendChild(sec(`Usage over time (${rangeInfo(state.range).label})`, chartBox));
    requestAnimationFrame(() => lineChart(chartBox, points, [{ key, name: c.name, values: seriesFor(points, state.range, key), slot: 0 }], { height: 180, endLabels: false }));
    if (c.aka && c.aka.length) {
      body.appendChild(sec("How the data source names it", el("p", { class: "fine" }, c.aka.map((n, i) => [i ? ", " : "", el("span", { class: "mono-text", text: n })]))));
    }
    body.appendChild(sec("Picture", el("p", { class: "fine", text: c.credit ||
      "Initials badge: no matching openly licensed picture yet. You can add your own picture (see README)." })));
  }

  // ================================================================== HISTORY
  function renderHistory() {
    $("#hKind").value = state.kind;
    rangeSeg($("#hRangeSeg"), renderHistory);
    if (!state.hBase || !resolveSpecExists(state.hBase)) state.hBase = findSnapshotAgo(24 * 7) || "";
    snapshotOptions($("#cmpBase"), state.hBase, { blank: "Choose a baseline…" });
    snapshotOptions($("#cmpCurrent"), state.hCurrent || "latest");
    const per = clear($("#hPeriod"));
    PERIODS.forEach(([v, label]) => per.appendChild(el("option", { value: v, text: label })));
    per.value = state.period;
    renderPresets();
    renderCompare();
    renderChart();
  }
  function resolveSpecExists(spec) {
    const snaps = snapsOf(state.kind);
    if (spec.startsWith("ver:")) return snaps.some(s => s.v === spec.slice(4));
    if (spec.startsWith("snap:")) return snaps.some(s => s.id === spec.slice(5));
    return spec === "latest";
  }
  function findSnapshotAgo(hours) {
    const snaps = snapsOf(state.kind);
    if (snaps.length < 2) return null;
    const target = new Date(snaps[0].t).getTime() - hours * 3600e3;
    let best = null;
    for (const s of snaps.slice(1)) {
      const diff = Math.abs(new Date(s.t).getTime() - target);
      if (!best || diff < best.diff) best = { s, diff };
    }
    return best ? `snap:${best.s.id}` : null;
  }
  function renderPresets() {
    const box = clear($("#comparePresets"));
    const snaps = snapsOf(state.kind);
    const versions = SITE.versions.map(v => v.version).filter(v => snaps.some(s => s.v === v)).reverse();
    const presets = [];
    if (snaps.length >= 2) {
      presets.push(["Previous snapshot", `snap:${snaps[1].id}`, "latest"]);
      presets.push(["1 day ago", findSnapshotAgo(24), "latest"]);
      presets.push(["7 days ago", findSnapshotAgo(24 * 7), "latest"]);
    }
    if (versions.length >= 2) presets.push([`${versions[1]} vs ${versions[0]}`, `ver:${versions[1]}`, `ver:${versions[0]}`]);
    presets.forEach(([label, base, cur]) => {
      const active = state.hBase === base && state.hCurrent === cur;
      const b = el("button", { type: "button", class: active ? "btn btn-primary" : "btn btn-ghost", "aria-pressed": String(active) }, label);
      b.addEventListener("click", () => { state.hBase = base; state.hCurrent = cur; renderHistory(); });
      box.appendChild(b);
    });
  }
  function renderCompare() {
    const movers = clear($("#movers")), banners = clear($("#cmpBanners")), tableBox = clear($("#compareTable"));
    if (!state.hBase) {
      movers.appendChild(el("div", { class: "empty-state", text: snapsOf(state.kind).length > 1 ? "Choose a baseline to compare with." : "Comparisons become available once at least two snapshots exist." }));
      return;
    }
    const d = computeCompare(state.kind, state.range, state.hBase, state.hCurrent);
    const label = sel => sel.mode === "version" ? `${sel.version} (${sel.snaps.length} snapshots)` : fmtDateLong(sel.snaps[0].t);
    banners.appendChild(banner("info", `${label(d.bSel)} → ${label(d.cSel)}.`,
      `${d.base.total.toLocaleString()} vs ${d.cur.total.toLocaleString()} teams in ${rangeInfo(state.range).label}. Changes are in percentage points (pp).`));
    if (!d.base.total || !d.cur.total) {
      movers.appendChild(el("div", { class: "empty-state", text: "One of the two selections has no teams in this range." }));
      return;
    }
    const withChange = d.rows.filter(r => r.delta_pp !== null);
    const risers = withChange.filter(r => r.delta_pp > 0).sort((a, b) => b.delta_pp - a.delta_pp).slice(0, 10);
    const fallers = withChange.filter(r => r.delta_pp < 0).sort((a, b) => a.delta_pp - b.delta_pp).slice(0, 10);
    const tierChanges = d.rows.filter(r => r.tier !== r.base_tier).sort((a, b) => TIERS.indexOf(a.tier) - TIERS.indexOf(b.tier));
    const moverRow = r => {
      const b = el("button", { class: "mover", type: "button" }, emblem(r, "sm"),
        el("div", { style: { "min-width": "0" } }, el("div", { class: "mn", text: r.name }),
          el("div", { class: "ms", text: `${fmtPct(r.base_pct)} → ${fmtPct(r.pct)}` })),
        el("div", { style: { "text-align": "right" } }, el("div", { class: "mv" }, deltaNode(r.delta_pp)),
          r.base_tier !== r.tier ? el("div", { class: "tier-move", text: `${r.base_tier} → ${r.tier} tier` }) : null));
      b.addEventListener("click", () => openDrawer(r.key));
      return b;
    };
    movers.appendChild(el("div", { class: "mover-col" }, el("h3", null, el("span", { class: "delta up" }, el("span", { class: "tri" })), "Rising"),
      risers.length ? risers.map(moverRow) : el("p", { class: "fine", text: "Nothing rose." })));
    movers.appendChild(el("div", { class: "mover-col" }, el("h3", null, el("span", { class: "delta down" }, el("span", { class: "tri" })), "Falling"),
      fallers.length ? fallers.map(moverRow) : el("p", { class: "fine", text: "Nothing fell." })));
    if (tierChanges.length) movers.appendChild(el("div", { class: "mover-col" }, el("h3", { text: "Tier changes" }), tierChanges.map(moverRow)));

    const rows = d.rows.slice().sort((a, b) => (b.delta_pp ?? -1e9) - (a.delta_pp ?? -1e9));
    const tbody = el("tbody");
    rows.forEach(r => {
      const tr = el("tr", { tabindex: "0" },
        el("td", null, el("div", { class: "cell-creature" }, emblem(r, "sm"), el("div", null, el("div", { class: "n", text: r.name }), el("div", { class: "s" }, rarityText(r.rarity))))),
        el("td", { class: "num", text: fmtPct(r.base_pct) }), el("td", { class: "num", text: fmtPct(r.pct) }),
        el("td", { class: "num" }, deltaNode(r.delta_pp)), el("td", null, tierBadge(r.base_tier), " → ", tierBadge(r.tier)));
      tr.addEventListener("click", () => openDrawer(r.key));
      tbody.appendChild(tr);
    });
    tableBox.appendChild(el("div", { class: "table-wrap" }, el("table", { class: "data" },
      el("thead", null, el("tr", null, el("th", { text: "Creature" }), el("th", { class: "num", text: "Baseline" }),
        el("th", { class: "num", text: "Compared with" }), el("th", { class: "num", text: "Change" }), el("th", { text: "Tier" }))),
      tbody)));
  }

  function renderChart() {
    const points = periodSnaps(state.kind);
    let keys = (state.chartKeys || []).filter(k => SITE.creatures[k]);
    if (!keys.length) {
      const latest = snapsOf(state.kind)[0];
      const u = latest && latest.u[state.range];
      keys = u ? Object.keys(u.c).slice(0, MAX_SERIES) : [];
    }
    Object.keys(colorSlots).forEach(k => { if (!keys.includes(k)) delete colorSlots[k]; });
    keys.forEach(assignSlot);
    HISTORY = { points, series: keys.map(k => ({ key: k, name: creature(k).name, values: seriesFor(points, state.range, k), slot: colorSlots[k] })) };
    const dl = clear($("#creatureList"));
    Object.values(SITE.creatures).map(c => c.name).sort().forEach(n => dl.appendChild(el("option", { value: n })));
    drawChart();
  }
  function assignSlot(key) {
    if (colorSlots[key] !== undefined) return colorSlots[key];
    const used = new Set(Object.values(colorSlots));
    for (let i = 0; i < MAX_SERIES; i++) if (!used.has(i)) { colorSlots[key] = i; return i; }
    colorSlots[key] = 0;
    return 0;
  }
  function drawChart() {
    const wrap = $("#chartWrap"), legend = clear($("#chartLegend"));
    HISTORY.series.forEach(s => {
      const b = el("button", { type: "button", style: { "--c": `var(--series-${s.slot + 1})` }, title: `Remove ${s.name} from the chart` },
        el("span", { class: "key" }), s.name, el("span", { class: "x", "aria-hidden": "true", text: "×" }));
      b.addEventListener("click", () => {
        state.chartKeys = HISTORY.series.map(x => x.key).filter(k => k !== s.key);
        if (!state.chartKeys.length) state.chartKeys = null;
        save();
        renderChart();
      });
      legend.appendChild(b);
    });
    if (!HISTORY.points.length) {
      clear(wrap).appendChild(el("div", { class: "empty-state", text: "No snapshots yet." }));
      return;
    }
    lineChart(wrap, HISTORY.points, HISTORY.series, { height: 300, endLabels: true });
    const box = clear($("#chartTable"));
    const tbody = el("tbody");
    HISTORY.points.slice().reverse().forEach((p, ri) => {
      const i = HISTORY.points.length - 1 - ri;
      tbody.appendChild(el("tr", null, el("td", { text: fmtDateLong(p.t) }), el("td", { text: p.v || "—" }),
        HISTORY.series.map(s => el("td", { class: "num", text: fmtPct(s.values[i]) }))));
    });
    box.appendChild(el("div", { class: "table-wrap" }, el("table", { class: "data" },
      el("thead", null, el("tr", null, el("th", { text: "Snapshot" }), el("th", { text: "Version" }),
        HISTORY.series.map(s => el("th", { class: "num", text: s.name })))), tbody)));
  }

  // Line chart: one y-axis (usage %), time on x, version/season markers, crosshair tooltip.
  function lineChart(wrap, points, series, opts) {
    clear(wrap);
    const W = Math.max(300, wrap.clientWidth || 600);
    const H = opts.height;
    const labelSpace = opts.endLabels && series.length <= 4 && W > 560 ? 128 : 16;
    const m = { l: 40, r: labelSpace, t: 18, b: 26 };
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const times = points.map(p => new Date(p.t).getTime());
    const t0 = times[0], t1 = times[times.length - 1];
    const span = Math.max(1, t1 - t0);
    const allVals = series.flatMap(s => s.values.filter(v => v !== null && v !== undefined));
    const yMax = Math.max(10, Math.ceil((Math.max(0, ...allVals) + 2) / 10) * 10);
    const x = t => points.length === 1 ? m.l + pw / 2 : m.l + ((t - t0) / span) * pw;
    const y = v => m.t + ph - (v / yMax) * ph;
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
      "aria-label": `Usage over time for ${series.map(s => s.name).join(", ")}` });
    const grid = svg("g", { class: "grid" }), axis = svg("g", { class: "axis" });
    const step = yMax <= 20 ? 5 : yMax <= 50 ? 10 : 20;
    for (let v = 0; v <= yMax; v += step) {
      grid.appendChild(svg("line", { x1: m.l, x2: m.l + pw, y1: y(v), y2: y(v) }));
      axis.appendChild(svg("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" }, `${v}%`));
    }
    const nTicks = Math.min(6, points.length);
    const seenDays = new Set();
    for (let i = 0; i < nTicks; i++) {
      const t = nTicks === 1 ? t0 : t0 + (span * i) / (nTicks - 1);
      const label = dayFmt.format(new Date(t));
      if (seenDays.has(label)) continue;
      seenDays.add(label);
      axis.appendChild(svg("text", { x: x(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === nTicks - 1 ? "end" : "middle" }, label));
    }
    root.append(grid, axis, svg("line", { class: "baseline", x1: m.l, x2: m.l + pw, y1: y(0), y2: y(0) }));
    const markers = svg("g", { class: "marker" });
    for (let i = 1; i < points.length; i++) {
      const vChange = points[i].v && points[i - 1].v && points[i].v !== points[i - 1].v;
      const sChange = points[i].lb !== points[i - 1].lb;
      if (!vChange && !sChange) continue;
      const mx = (x(times[i]) + x(times[i - 1])) / 2;
      markers.appendChild(svg("line", { x1: mx, x2: mx, y1: m.t, y2: m.t + ph }));
      markers.appendChild(svg("text", { x: mx + 4, y: m.t - 5 }, vChange ? points[i].v : "new season"));
    }
    root.appendChild(markers);
    const gSeries = svg("g", { class: "series" });
    const ends = [];
    series.forEach(s => {
      const color = `var(--series-${s.slot + 1})`;
      let d = "", pen = false;
      s.values.forEach((v, i) => {
        if (v === null || v === undefined) { pen = false; return; }
        d += (pen ? "L" : "M") + x(times[i]).toFixed(1) + " " + y(v).toFixed(1);
        pen = true;
      });
      gSeries.appendChild(svg("path", { d, style: `stroke:${color}` }));
      for (let i = s.values.length - 1; i >= 0; i--) {
        if (s.values[i] !== null && s.values[i] !== undefined) {
          gSeries.appendChild(svg("circle", { class: "end", cx: x(times[i]), cy: y(s.values[i]), r: 4, style: `fill:${color}` }));
          ends.push({ s, yv: y(s.values[i]), x: x(times[i]), v: s.values[i] });
          break;
        }
      }
    });
    root.appendChild(gSeries);
    if (labelSpace > 16) {
      const placed = [];
      ends.sort((a, b) => a.yv - b.yv).forEach(e => {
        if (placed.some(p => Math.abs(p - e.yv) < 14)) return; // never stack colliding labels; the legend covers them
        placed.push(e.yv);
        const name = e.s.name.length > 14 ? e.s.name.slice(0, 13) + "…" : e.s.name;
        root.appendChild(svg("text", { class: "elabel", x: e.x + 9, y: e.yv + 4 }, `${name} ${fmtPct(e.v)}`));
      });
    }
    const cross = svg("line", { class: "cross", y1: m.t, y2: m.t + ph, visibility: "hidden" });
    const hoverDots = svg("g");
    root.append(cross, hoverDots);
    const hit = svg("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent", tabindex: "0",
      "aria-label": "Chart area. Use left and right arrow keys to read values." });
    root.appendChild(hit);
    wrap.appendChild(root);
    let idx = points.length - 1;
    const hide = () => { cross.setAttribute("visibility", "hidden"); clear(hoverDots); hideTip(); };
    const showAt = (i, cx, cy) => {
      idx = Math.max(0, Math.min(points.length - 1, i));
      const px = x(times[idx]);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
      clear(hoverDots);
      const rows = series.map(s => ({ s, v: s.values[idx] })).filter(r => r.v !== null && r.v !== undefined).sort((a, b) => b.v - a.v);
      rows.forEach(r => hoverDots.appendChild(svg("circle", { class: "hover-dot", cx: px, cy: y(r.v), r: 4.5, style: `fill:var(--series-${r.s.slot + 1})` })));
      const p = points[idx];
      const content = el("div", null,
        el("div", { class: "tt-title", text: `${fmtDateLong(p.t)} · ${p.v || "?"}` }),
        rows.length ? rows.map(r => el("div", { class: "tt-row" }, el("span", { class: "key", style: { background: `var(--series-${r.s.slot + 1})` } }),
          el("span", { class: "tv", text: fmtPct(r.v) }), el("span", { class: "tn", text: r.s.name })))
          : el("div", { class: "tt-title", text: "No teams in this range" }));
      const box = root.getBoundingClientRect();
      showTip(cx !== undefined ? cx : box.left + (px / W) * box.width, cy !== undefined ? cy : box.top + 30, content);
    };
    const nearest = clientX => {
      const box = root.getBoundingClientRect();
      const sx = ((clientX - box.left) / box.width) * W;
      let best = 0;
      times.forEach((t, i) => { if (Math.abs(x(t) - sx) < Math.abs(x(times[best]) - sx)) best = i; });
      return best;
    };
    hit.addEventListener("pointermove", ev => showAt(nearest(ev.clientX), ev.clientX, ev.clientY));
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", () => showAt(idx));
    hit.addEventListener("blur", hide);
    hit.addEventListener("keydown", ev => {
      if (ev.key === "ArrowLeft") { showAt(idx - 1); ev.preventDefault(); }
      if (ev.key === "ArrowRight") { showAt(idx + 1); ev.preventDefault(); }
    });
  }

  // ================================================================== wiring
  function wire() {
    window.addEventListener("hashchange", showView);
    $("#selData").addEventListener("change", e => { state.sel = e.target.value; renderTiers(); });
    const setKind = v => { state.kind = v; state.sel = "latest"; state.hBase = ""; state.hCurrent = "latest"; ensureRange(v); save(); reloadCurrentView(); };
    $("#selKind").addEventListener("change", e => setKind(e.target.value));
    $("#hKind").addEventListener("change", e => setKind(e.target.value));
    $("#selSort").addEventListener("change", e => { state.sort = e.target.value; save(); renderTierContent(); });
    let searchTimer = null;
    $("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(renderTierContent, 120); });
    $$("#viewToggle button").forEach(b => b.addEventListener("click", () => { state.view = b.dataset.view; save(); renderTierContent(); }));
    $("#cmpBase").addEventListener("change", e => { state.hBase = e.target.value; renderPresets(); renderCompare(); });
    $("#cmpCurrent").addEventListener("change", e => { state.hCurrent = e.target.value; renderPresets(); renderCompare(); });
    $("#hPeriod").addEventListener("change", e => { state.period = e.target.value; save(); renderChart(); });
    $("#chartAdd").addEventListener("change", e => {
      const q = norm(e.target.value.trim());
      e.target.value = "";
      const entries = Object.entries(SITE.creatures);
      const hit = entries.find(([, c]) => norm(c.name) === q) || entries.find(([, c]) => norm(c.name).startsWith(q));
      if (!hit) return toast("No creature with that name in the data.");
      const keys = HISTORY ? HISTORY.series.map(s => s.key) : [];
      if (keys.includes(hit[0])) return toast(`${hit[1].name} is already on the chart.`);
      if (keys.length >= MAX_SERIES) return toast(`Up to ${MAX_SERIES} creatures at a time — remove one first.`);
      state.chartKeys = keys.concat(hit[0]);
      save();
      renderChart();
    });
    $("#chartTableToggle").addEventListener("click", () => {
      const t = $("#chartTable"), showing = t.hidden;
      t.hidden = !showing;
      $("#chartWrap").hidden = showing;
      $("#chartTableToggle").textContent = showing ? "Show as chart" : "Show as table";
    });
    $("#themeToggle").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("jwa-theme", next); } catch (e) { /* ignore */ }
      if (currentView() === "history" && HISTORY) drawChart();
    });
    $$("#drawer [data-close]").forEach(n => n.addEventListener("click", closeDrawer));
    document.addEventListener("keydown", e => { if (e.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer(); });
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => { if (currentView() === "history" && HISTORY && !$("#chartWrap").hidden) drawChart(); }, 150);
    });
  }

  function applyMode() {
    $("#exampleBanner").hidden = !SITE.demo;
    $(".brand-sub").textContent = isWebsite() ? "Top 100 arena usage · updated twice a day" : "Private · runs on this computer";
    document.title = "JWA Meta Tracker";
  }

  async function init() {
    wire();
    try {
      await loadSite();
    } catch (e) {
      clear($("#tierContent")).appendChild(banner("error", "Could not load the data.", e.message));
      return;
    }
    if (!isWebsite()) {
      try {
        const resp = await fetch("api/status", { cache: "no-store" });
        if (resp.ok) LOCAL = await resp.json();
      } catch (e) { LOCAL = null; }
    }
    applyMode();
    renderStatusPill();
    showView();
    if (LOCAL) {
      setInterval(() => { if (!document.hidden) refreshLocalStatus(); }, (LOCAL.collection_running || !snapsOf("arena").length) ? 4000 : 20000);
    } else {
      setInterval(async () => {
        if (document.hidden) return;
        const before = SITE.generated_at;
        try { await loadSite(); } catch (e) { return; }
        if (SITE.generated_at !== before) { toast("New data has been published."); reloadCurrentView(); }
        renderStatusPill();
      }, 15 * 60e3);
      setInterval(renderStatusPill, 60e3);
    }
  }
  init();
})();
