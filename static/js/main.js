/* PowerSOC dashboard frontend. Depends on chart.min.js (Chart.js 4) and
   theme.js. All data comes from the JSON API ({success,data,error}). */
const PowerSOC = (function () {
    "use strict";

    const charts = {};            // id -> Chart instance
    let currentDate = null;       // YYYY-MM-DD currently shown
    let showingToday = true;      // is the dashboard on today? (drives live SoC)
    let lastSummary = {};         // last day summary (for CO2-avoided estimate)

    // ---- helpers ----
    function cssVar(name) {
        return getComputedStyle(document.documentElement)
            .getPropertyValue(name).trim() || "#888";
    }
    function alpha(hex, a) {
        // #rrggbb -> rgba()
        const m = /^#?([0-9a-f]{6})$/i.exec(hex);
        if (!m) return hex;
        const n = parseInt(m[1], 16);
        return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    }
    function fmtKwh(v) { return (v == null) ? "–" : (+v).toFixed(1) + " kWh"; }
    function fmtPct(v) { return (v == null) ? "–" : Math.round(v) + "%"; }
    function fmtMoney(pence) {
        if (pence == null) return "–";
        const pounds = pence / 100;
        return (pounds < 0 ? "−£" : "£") + Math.abs(pounds).toFixed(2);
    }
    // Escape user/upstream-controlled strings before they go into innerHTML.
    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
            ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    async function api(path) {
        const r = await fetch(path, { headers: { "Accept": "application/json" } });
        const body = await r.json();
        if (!body.success) throw new Error(body.error || "request failed");
        // The service worker answers from its cache when the server is
        // unreachable and says so in this header: the page must show that
        // data as stale, not "updated <now>".
        if (r.headers.get("X-PowerSOC-Offline") && body.data && typeof body.data === "object")
            body.data._offline = true;
        return body.data;
    }

    // ---- theme-aware colours ----
    function seriesColor(key) { return cssVar("--c-" + key); }

    function chartBaseOptions(stacked, yLabel) {
        const grid = alpha(cssVar("--border"), 0.6);
        const text = cssVar("--text-muted");
        return {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: "index", intersect: false },
            plugins: { legend: { labels: { color: text } } },
            scales: {
                x: { stacked: stacked, ticks: { color: text, maxTicksLimit: 12 },
                     grid: { color: grid } },
                y: { stacked: stacked, beginAtZero: true,
                     title: { display: !!yLabel, text: yLabel, color: text },
                     ticks: { color: text }, grid: { color: grid } },
            },
        };
    }

    function applyTheme() {
        Chart.defaults.color = cssVar("--text-muted");
        Object.values(charts).forEach((c) => {
            // Recolour datasets from their stored colour-key, then update.
            c.data.datasets.forEach((ds) => {
                if (ds._ckey) {
                    const col = seriesColor(ds._ckey);
                    ds.borderColor = col;
                    ds.backgroundColor = ds._fill ? alpha(col, 0.25) : col;
                }
            });
            const stacked = c.options.scales.x.stacked;
            // Rebuilding options wipes per-chart overrides — carry the pinned
            // y-max (the SoC chart's 100%) across the theme switch.
            const yMax = c.options.scales.y.max;
            c.options = Object.assign(chartBaseOptions(stacked,
                c.options.scales.y.title.text), { animation: false });
            if (yMax != null) c.options.scales.y.max = yMax;
            c.update();
        });
    }

    function ds(label, data, ckey, opts) {
        const col = seriesColor(ckey);
        return Object.assign({
            label: label, data: data, _ckey: ckey,
            borderColor: col, backgroundColor: col, borderWidth: 1.5,
        }, opts || {});
    }

    function makeChart(id, type, labels, datasets, stacked, yLabel) {
        const el = document.getElementById(id);
        if (!el) return;
        // Update in place when the chart already exists and the dataset count
        // matches — avoids the flicker of destroy+recreate every poll. Falls
        // back to a full rebuild if the shape changed.
        const existing = charts[id];
        if (existing && existing.config.type === type
                     && existing.data.datasets.length === datasets.length) {
            existing.data.labels = labels;
            datasets.forEach((ds, i) => {
                const cur = existing.data.datasets[i];
                cur.data = ds.data;
                cur.label = ds.label;
                // A different series in the same slot (History: Electricity ->
                // Gas) kept the old colour when only data/label were copied.
                cur._ckey = ds._ckey; cur._fill = ds._fill;
                cur.borderColor = ds.borderColor;
                cur.backgroundColor = ds.backgroundColor;
            });
            existing.update("none");   // no animation on refresh
            return;
        }
        if (existing) existing.destroy();
        charts[id] = new Chart(el, {
            type: type, data: { labels: labels, datasets: datasets },
            options: chartBaseOptions(stacked, yLabel),
        });
    }

    // ---- rendering ----
    function deltaVsAvg(value, avg) {
        // "▲ 12% vs 7-day avg" — small context marker for the summary cards.
        if (value == null || avg == null || avg <= 0) return "";
        const pct = Math.round((value - avg) / avg * 100);
        if (Math.abs(pct) < 5) return " · ≈7-day avg";
        return ` · ${pct > 0 ? "▲" : "▼"} ${Math.abs(pct)}% vs 7-day avg`;
    }

    // One label for how firm the day's cost is (server-side cost_status):
    // "confirmed" used to show whenever export wasn't estimated — including
    // CT-estimated, partial and empty days.
    const COST_LABEL = {
        confirmed: "confirmed · after export credit",
        provisional: "provisional · still settling",
        unpriced: "tariff rates unavailable",
        no_data: "no data for this day",
    };

    function renderCards(s, avg7, costStatus) {
        lastSummary = s || {};
        avg7 = avg7 || null;
        const $ = (id) => document.getElementById(id);
        // Net cost -> Net Gain (green) when we're in credit for the day.
        const net = s.net_cost_pence;
        const gain = net != null && net < 0;
        const card = $("card-net");
        $("c-net-label").textContent = gain ? "💷 Net gain" : "💷 Net cost";
        $("c-net").textContent = net == null ? "–" : fmtMoney(Math.abs(net));
        card.classList.toggle("gain", gain);
        card.style.setProperty("--stripe", gain ? "var(--accent-2)" : "var(--accent)");
        let label = COST_LABEL[costStatus] || COST_LABEL.provisional;
        if (costStatus === "provisional")
            label = "provisional · " + (s.import_estimated ? "est. import"
                : s.export_estimated ? "est. export" : "still settling");
        $("c-net-sub").textContent = label
            + (avg7 ? deltaVsAvg(net, avg7.net_cost_pence) : "");
        $("c-pv").textContent = fmtKwh(s.pv_kwh);
        const pvSub = $("c-pv-sub");
        // Always rewritten: on a past day (no avg7) it kept today's text.
        if (pvSub) pvSub.textContent = avg7
            ? deltaVsAvg(s.pv_kwh, avg7.pv_kwh).replace(/^ · /, "") : "";
        // Export box
        $("c-export").textContent = fmtKwh(s.grid_export_kwh);
        $("c-export-sub").textContent =
            (s.export_earned_pence ? "earned " + fmtMoney(s.export_earned_pence) : "")
            + (s.export_estimated ? " (est.)" : "");
        $("c-selfsuff").textContent = fmtPct(s.self_sufficiency_pct);
        // Self-consumption = PV used on-site / PV generated.
        const sc = (s.pv_kwh && s.pv_kwh > 0)
            ? Math.max(0, Math.min(100, (s.pv_kwh - (s.grid_export_kwh || 0)) / s.pv_kwh * 100))
            : null;
        $("c-selfsuff-sub").textContent = sc != null
            ? "self-consumption " + Math.round(sc) + "%" : "grid vs house load";
        // Battery level: end-of-day for past days; on today, pollLive overwrites
        // this with the live current SoC.
        $("c-soc").textContent = fmtPct(s.end_soc_pct);
        $("c-soc-sub").textContent = s.peak_soc_pct != null
            ? "peak " + fmtPct(s.peak_soc_pct) + (showingToday ? " today" : "") : "";
        $("c-import").textContent = fmtKwh(s.grid_import_kwh);
        const n1 = (v) => (v == null ? "–" : (+v).toFixed(1));
        $("c-import-sub").textContent = s.grid_import_kwh == null ? "" :
            `cheap ${n1(s.cheap_import_kwh)} · day ${n1(s.day_import_kwh)} · `
            + `peak ${n1(s.peak_import_kwh)}`;
        $("c-gas").textContent = fmtKwh(s.gas_kwh);
        $("c-gas-sub").textContent = s.gas_cost_pence != null
            ? "cost " + fmtMoney(s.gas_cost_pence) : "";
    }

    function renderCharts(p) {
        const L = p.labels;
        makeChart("chart-flow", "line", L, [
            ds("Solar PV", p.solar.pv, "solar", { fill: true, _fill: true, tension: 0.3, pointRadius: 0 }),
            ds("Battery discharge", p.solar.battery_discharge, "battery", { fill: true, _fill: true, tension: 0.3, pointRadius: 0 }),
            ds("Grid import", p.solar.grid_import, "import", { fill: true, _fill: true, tension: 0.3, pointRadius: 0 }),
        ], true, "kWh / half-hour");

        makeChart("chart-soc", "line", L, [
            ds("Battery SoC", p.solar.soc, "battery", { tension: 0.3, pointRadius: 0, spanGaps: true }),
        ], false, "%");
        charts["chart-soc"].options.scales.y.max = 100;
        charts["chart-soc"].update();

        makeChart("chart-load", "bar", L, [
            ds("House load", p.solar.house_load, "import"),
        ], false, "kWh");

        makeChart("chart-cost", "bar", L, [
            ds("Cheap", p.electricity.cheap, "cheap"),
            ds("Day", p.electricity.day, "day"),
            ds("Peak", p.electricity.peak, "peak"),
        ], true, "pence");

        makeChart("chart-export", "bar", L, [
            ds("Export", p.export.export_kwh, "export"),
        ], false, "kWh");
        const exTitle = document.getElementById("title-export");
        if (exTitle) exTitle.textContent = p.export.estimated ? "Export (est.)" : "Export";

        makeChart("chart-gas", "bar", L, [
            ds("Gas", p.gas.gas_kwh, "gas"),
        ], false, "kWh");
    }

    function updateNav(p) {
        const nav = p.nav;
        showingToday = !!nav.is_today;
        document.getElementById("nav-date").value = p.date;
        document.getElementById("day-title").textContent =
            nav.is_today ? "Today" : p.date;
        const prev = document.getElementById("nav-prev");
        const next = document.getElementById("nav-next");
        prev.disabled = !nav.prev; next.disabled = !nav.next;
        prev.dataset.date = nav.prev || ""; next.dataset.date = nav.next || "";
        document.getElementById("no-data").style.display =
            p.has_data ? "none" : "";
    }

    let daySeq = 0;   // request sequencing: a stale response must never win

    async function loadDay(date) {
        const my = ++daySeq;
        const path = date ? ("/api/day/" + date) : "/api/today";
        try {
            const p = await api(path);
            if (my !== daySeq) return;   // a newer navigation superseded this
            currentDate = p.date;        // only on success — a failed nav must
            updateNav(p);                // not move the pointer
            renderCards(p.summary || {}, p.avg7, p.cost_status);
            renderCharts(p);
            const off = document.getElementById("offline-chip");
            if (off) off.style.display = p._offline ? "" : "none";
        } catch (e) {
            console.error("loadDay failed", e);
        }
    }

    // ---- backfill banner ----
    let backfillWasRunning = false;

    async function pollBackfill() {
        try {
            const s = await api("/api/backfill/status");
            const banner = document.getElementById("backfill-banner");
            if (!banner) return;
            if (s.running) {
                backfillWasRunning = true;
                banner.style.display = "";
                document.getElementById("backfill-progress").textContent =
                    s.days_total ? `${s.days_done}/${s.days_total} days` : "";
            } else {
                if (backfillWasRunning) {
                    backfillWasRunning = false;
                    loadDay(currentDate);   // refresh once backfill finishes
                }
                banner.style.display = "none";
            }
        } catch (e) { /* ignore */ }
        // Keep polling at a low idle cadence — a backfill started from another
        // tab (or the API) should still surface here without a reload.
        setTimeout(pollBackfill, backfillWasRunning ? 3000 : 60000);
    }

    // ---- live panel ----
    function fmtW(w) {
        if (w == null) return "–";
        return Math.abs(w) >= 1000 ? (w / 1000).toFixed(2) + " kW"
                                   : Math.round(w) + " W";
    }
    // Timer ids so the visibility hook can refresh now without piling up
    // duplicate schedules.
    let liveTimer = null, todayTimer = null;

    let liveFails = 0;   // consecutive failed polls -> show the stale chip

    function markLiveStale() {
        const ts = document.getElementById("live-ts");
        if (ts && !ts.textContent.startsWith("⚠️")) {
            ts.textContent = "⚠️ stale — last " + ts.textContent;
            ts.style.color = "var(--c-peak)";
        }
    }

    async function pollLive() {
        if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
        try {
            const d = await api("/api/live");
            const panel = document.getElementById("live-panel");
            if (panel && d.sunsynk) {
                const s = d.sunsynk, $ = (i) => document.getElementById(i);
                panel.style.display = "";
                const absW = (w) => (w == null ? null : Math.abs(w));
                $("live-pv").textContent = fmtW(s.pv_w);
                $("live-grid").textContent = fmtW(absW(s.grid_w));
                $("live-grid-label").textContent =
                    s.grid_dir === "export" ? "Grid export" : "Grid import";
                $("live-grid").style.color =
                    s.grid_dir === "export" ? "var(--c-export)" : "var(--c-import)";
                $("live-batt").textContent = fmtW(absW(s.battery_w));
                $("live-batt-label").textContent =
                    s.battery_dir === "charge" ? "Battery charging"
                    : s.battery_dir === "discharge" ? "Battery discharging" : "Battery idle";
                // soc_pct can legitimately be null (empty battery sub-payload).
                $("live-soc").textContent =
                    s.soc_pct != null ? "SoC " + Math.round(s.soc_pct) + "%" : "SoC –";
                $("live-load").textContent = fmtW(s.load_w);
                // Time to reserve / full at the current battery power (QW2).
                const eta = d.battery_eta, etaEl = $("live-eta");
                if (etaEl) etaEl.textContent = !eta ? "" : (() => {
                    const h = Math.floor(eta.minutes / 60), m = eta.minutes % 60;
                    const dur = h ? `${h}h${String(m).padStart(2, "0")}m` : `${m} min`;
                    const at = new Date(eta.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
                    return eta.kind === "full" ? `full in ≈${dur} (${at})`
                        : `≈${dur} to ${eta.reserve_pct}% reserve (${at})`;
                })();
                const t = s.today || {};
                const n1 = (v) => (v != null ? (+v).toFixed(1) : "–");
                $("live-today").textContent =
                    `Today so far — solar ${n1(t.pv_kwh)} · import ${n1(t.import_kwh)}`
                    + ` · export ${n1(t.export_kwh)} · charge ${n1(t.charge_kwh)}`
                    + ` · discharge ${n1(t.discharge_kwh)} kWh`;
                if (d.stale || d._offline) {
                    markLiveStale();
                } else {
                    liveFails = 0;
                    const ts = $("live-ts");
                    ts.textContent = "updated " + new Date().toLocaleTimeString();
                    ts.style.color = "";
                }
                // Browser tab shows current house usage.
                if (s.load_w != null) document.title = fmtW(s.load_w) + " · PowerSOC";
                // Reflect the live battery level in the summary card (today only).
                if (showingToday && s.soc_pct != null) {
                    const cs = document.getElementById("c-soc");
                    if (cs) cs.textContent = Math.round(s.soc_pct) + "%";
                }
            } else if (panel && panel.style.display === "none") {
                // No live data yet and never loaded -> leave hidden. If we had
                // data before, keep showing the last values (don't blank out).
            }
            const tw = d.tariff, el = document.getElementById("live-tariff");
            if (tw && el) {
                const rate = tw.import_rate_p != null ? ` · ${tw.import_rate_p.toFixed(1)}p` : "";
                // Countdown to the next window boundary — the most actionable
                // number for load-shifting ("peak ends in 38m").
                let left = "";
                if (tw.window_until) {
                    const mins = Math.round((new Date(tw.window_until) - Date.now()) / 60000);
                    if (mins > 0) left = mins < 100 ? ` · ${mins}m left`
                        : ` · ${(mins / 60).toFixed(1)}h left`;
                }
                const hint = tw.export_gt_import ? " · export pays more ⤴" : "";
                el.textContent = `${tw.window} window${rate}${left}${hint}`;
                el.className = "pill pill-" + tw.window;
            }
        } catch (e) {
            if (++liveFails >= 2) markLiveStale();
        }
        liveTimer = setTimeout(pollLive, 60000);   // matches Sunsynk's ~1-min cadence
    }

    // Re-fetch today's day payload so the summary cards (Solar today, Import,
    // Export, Net cost...) and the intraday charts advance as new intervals
    // settle. Only runs while viewing today — historic days are immutable so
    // there's nothing to refresh.
    async function pollToday() {
        if (todayTimer) { clearTimeout(todayTimer); todayTimer = null; }
        if (showingToday) {
            try { await loadDay(null); } catch (e) { /* ignore transient */ }
        }
        todayTimer = setTimeout(pollToday, 300000);   // 5 min
    }

    // ---- tariff panel ----
    async function loadTariffs() {
        // Never retried before: a transient failure at page load left the
        // panel hidden until a reload.
        setTimeout(loadTariffs, 3600000);
        try {
            const t = await api("/api/tariffs");
            const panel = document.getElementById("tariff-panel");
            if (!panel || !t.configured) return;
            const p = (v) => v == null ? "–" : v.toFixed(2) + "p";
            const b = t.import_bands || {};
            const bt = t.band_times || {};
            // Band times come from the server, per tariff (they were Cosy's
            // hard-coded hours for every tariff).
            const cells = [
                ["Cheap", b.cheap, bt.cheap || "", "var(--c-cheap)"],
                ["Day", b.day, t.is_cosy === false ? (bt.day || "") : "other times", "var(--c-day)"],
                ["Peak", b.peak, bt.peak || "", "var(--c-peak)"],
                ["Gas", t.gas_p, "per kWh", "var(--c-gas)"],
                ["Export", t.export_p, "per kWh", "var(--c-export)"],
            ].filter(([label, val]) => val != null || !["Peak"].includes(label));
            document.getElementById("tariff-body").innerHTML = cells.map(
                ([label, val, sub, col]) =>
                `<div class="tariff-cell"><div class="label">${label}</div>`
                + `<div class="value" style="color:${col}">${p(val)}</div>`
                + `<div class="sub">${sub}</div></div>`).join("");
            const sc = [];
            if (t.standing_elec_p != null) sc.push(`elec ${p(t.standing_elec_p)}/day`);
            if (t.standing_gas_p != null) sc.push(`gas ${p(t.standing_gas_p)}/day`);
            let flex = "";
            if (t.flexible_p != null) {
                flex = `Flexible Octopus import is ${p(t.flexible_p)}/kWh`;
                if (b.cheap != null) {
                    const save = (t.flexible_p - b.cheap).toFixed(2);
                    flex += ` — your cheap rate saves ${save}p/kWh vs Flexible`;
                }
            }
            document.getElementById("tariff-flex").textContent =
                [sc.length ? "Standing: " + sc.join(" · ") : "", flex].filter(Boolean).join("  •  ");
            document.getElementById("tariff-region").textContent =
                t.region ? "(region " + t.region + ")" : "";
            panel.style.display = "";
        } catch (e) { /* leave hidden on error */ }
    }

    // ---- weather ----
    async function loadWeather() {
        const el = document.getElementById("wx");
        if (!el) return;
        try {
            const w = await api("/api/weather");
            if (w.available) {
                el.textContent = `${w.icon} ${Math.round(w.temp_c)}°C · ${w.condition} · ${w.location}`;
                el.title = `Feels ${Math.round(w.feels_c)}°C · wind ${Math.round(w.wind_kmh)} km/h · ${Math.round(w.humidity)}% RH`;
                el.style.display = "";
            }
        } catch (e) { /* hide on error */ }
        setTimeout(loadWeather, 1800000);   // 30 min
    }

    // ---- solar forecast + tips ----
    async function loadForecast() {
        setTimeout(loadForecast, 3600000);   // hourly — scheduled first so an
        try {                                // early return can't stop it
            const f = await api("/api/forecast");
            const panel = document.getElementById("forecast-panel");
            if (!panel) return;
            const icon = (o) => ({ high: "☀️", moderate: "⛅", low: "🌥️" }[o] || "🌡️");
            // esc() everything upstream-derived — condition/outlook are lookup
            // table values today, but this sink must not rely on that.
            const k = f.kwh || {};
            const kwhTxt = (v, r) => v == null ? "" : ` · ~${Math.round(v)} kWh`
                + (r ? ` <small class="muted">(${Math.round(r[0])}–${Math.round(r[1])})</small>` : "");
            const day = (label, d, kv, kr) => !d ? "" :
                `<div class="card summary-card"><div class="label">${label} · ${esc(d.condition)}</div>`
                + `<div class="value" style="font-size:1.3rem">${icon(d.outlook)} ${esc(d.outlook)} solar${kwhTxt(kv, kr)}</div>`
                + `<div class="sub">${d.temp_min != null ? Math.round(d.temp_min) + "–" + Math.round(d.temp_max) + "°C" : ""}`
                + `${d.precip_prob != null ? " · " + Number(d.precip_prob) + "% rain" : ""}</div></div>`;
            if (f.available) {
                document.getElementById("forecast-days").innerHTML =
                    day("Today", f.today, k.today_kwh, k.today_range)
                    + day("Tomorrow", f.tomorrow, k.tomorrow_kwh, k.tomorrow_range);
            }
            document.getElementById("forecast-tips").innerHTML =
                (f.tips || []).map((t) => `<li>${esc(t)}</li>`).join("");
            panel.style.display = "";
        } catch (e) { /* leave hidden */ }
    }

    // ---- tonight's charge plan ----
    async function loadPlan() {
        setTimeout(loadPlan, 1800000);   // 30 min (first: early returns skip the end)
        try {
            const p = await api("/api/plan");
            const panel = document.getElementById("plan-panel");
            if (!panel || !p.available) return;
            document.getElementById("plan-headline").textContent = p.headline || "";
            const reasons = (p.reasons || []).slice();
            if (p.charge_kwh != null) {
                reasons.push(`~${p.charge_kwh} kWh to add`
                    + (p.est_cost_pence != null ? ` (≈${fmtMoney(p.est_cost_pence)})` : ""));
            }
            document.getElementById("plan-reasons").innerHTML =
                reasons.map((r) => `<li>${esc(r)}</li>`).join("");
            panel.style.display = "";
            // v2: projected state of charge with vs without the plan.
            const box = document.getElementById("plan-chart-box");
            if (box && p.trajectory && p.trajectory.length) {
                box.style.display = "";
                makeChart("chart-plan", "line", p.trajectory.map((t) => t.label), [
                    ds("With plan", p.trajectory.map((t) => t.soc), "battery",
                       { pointRadius: 0, tension: 0.2 }),
                    ds("Without charging", p.trajectory.map((t) => t.soc_baseline), "peak",
                       { pointRadius: 0, tension: 0.2, borderDash: [4, 3] }),
                ], false, "SoC %");
                charts["chart-plan"].options.scales.y.max = 100;
                charts["chart-plan"].update();
            } else if (box) { box.style.display = "none"; }
        } catch (e) { /* leave hidden */ }
    }

    // ---- system payback / ROI ----
    async function loadRoi() {
        setTimeout(loadRoi, 3600000);
        try {
            const r = await api("/api/roi");
            const panel = document.getElementById("roi-panel");
            if (!panel || !r.configured) return;
            if (!r.available) {
                // No Flexible rate yet: an export-only "benefit" made the
                // payback look like decades. Say why instead.
                ["roi-pct", "roi-cum", "roi-daily", "roi-be"].forEach((id) => {
                    document.getElementById(id).textContent = "–";
                });
                document.getElementById("roi-be").title = r.reason || "";
                panel.style.display = "";
                return;
            }
            document.getElementById("roi-pct").textContent =
                (r.percent_paid_back != null ? r.percent_paid_back : "–") + "%";
            document.getElementById("roi-cum").textContent =
                "£" + r.cumulative_benefit_gbp + " of £" + r.system_cost_gbp;
            document.getElementById("roi-daily").textContent = "£" + r.avg_daily_gbp;
            let be = "–";
            if (r.percent_paid_back >= 100) {
                be = "Paid off 🎉";
            } else if (r.breakeven_days > 0) {
                const d = new Date(Date.now() + r.breakeven_days * 86400000);
                be = (r.breakeven_days > 365 ? (r.breakeven_days / 365).toFixed(1) + " yrs"
                    : r.breakeven_days + " days") + " (~" + d.toLocaleDateString() + ")";
            }
            document.getElementById("roi-be").textContent = be;
            panel.style.display = "";
        } catch (e) { /* leave hidden */ }
    }

    // ---- grid carbon ----
    function _carbonColor(idx) {
        return { "very low": "var(--c-cheap)", "low": "var(--c-cheap)",
                 "moderate": "var(--c-day)", "high": "var(--c-peak)",
                 "very high": "var(--c-peak)" }[idx] || "var(--text-muted)";
    }
    async function loadCarbon() {
        // Reschedule first: an unavailable result used to `return` before the
        // timer, so the panel never retried until a page reload.
        setTimeout(loadCarbon, 1800000);   // 30 min
        try {
            const c = await api("/api/carbon");
            const panel = document.getElementById("carbon-panel");
            if (!panel || !c.available) return;
            document.getElementById("carbon-region").textContent =
                c.region ? "(" + c.region + ")" : "";
            if (c.current) {
                const el = document.getElementById("carbon-now");
                el.textContent = Math.round(c.current.g) + " g/kWh";
                el.style.color = _carbonColor(c.current.index);
                document.getElementById("carbon-now-idx").textContent = c.current.index || "";
            }
            if (c.greenest) {
                const t = c.greenest.from
                    ? new Date(c.greenest.from).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "–";
                document.getElementById("carbon-green").textContent = t;
                document.getElementById("carbon-green-g").textContent =
                    Math.round(c.greenest.g) + " g/kWh · " + (c.greenest.index || "");
            }
            // CO2 avoided ≈ self-sufficient energy (load - grid import) × intensity.
            const s = lastSummary;
            if (s && s.house_load_kwh != null && c.current) {
                const selfKwh = Math.max(0, (s.house_load_kwh || 0) - (s.grid_import_kwh || 0));
                document.getElementById("carbon-saved").textContent =
                    (selfKwh * c.current.g / 1000).toFixed(2) + " kg";
            }
            panel.style.display = "";
        } catch (e) { /* leave hidden */ }
    }

    // ---- next-24h price + carbon timeline (quick win 7) ----
    let timelineChart = null;
    async function loadTimeline() {
        setTimeout(loadTimeline, 900000);   // 15 min
        try {
            const t = await api("/api/timeline");
            const panel = document.getElementById("timeline-panel");
            if (!panel || !t.slots.some((s) => s.import_p != null || s.carbon_g != null)) return;
            const bandCol = (b) => cssVar("--c-" + (b || "day"));
            const data = {
                labels: t.slots.map((s) => s.label),
                datasets: [
                    { type: "bar", label: "Import p/kWh", yAxisID: "y",
                      data: t.slots.map((s) => s.import_p),
                      // Assumed (repeated from a day earlier): paler.
                      backgroundColor: t.slots.map((s) => alpha(bandCol(s.band), s.assumed ? 0.35 : 0.75)) },
                    { type: "line", label: "Export p/kWh", yAxisID: "y", pointRadius: 0,
                      borderColor: cssVar("--c-export"), data: t.slots.map((s) => s.export_p),
                      borderDash: [4, 3] },
                    { type: "line", label: "gCO₂/kWh", yAxisID: "y2", pointRadius: 0, tension: 0.3,
                      borderColor: cssVar("--text-muted"), data: t.slots.map((s) => s.carbon_g) },
                ],
            };
            const base = chartBaseOptions(false, "p/kWh");
            base.scales.y2 = { position: "right", beginAtZero: true, grid: { drawOnChartArea: false },
                               title: { display: true, text: "gCO₂/kWh", color: cssVar("--text-muted") },
                               ticks: { color: cssVar("--text-muted") } };
            if (timelineChart) timelineChart.destroy();
            timelineChart = new Chart(document.getElementById("chart-timeline"),
                                      { type: "bar", data: data, options: base });
            const c = t.cheapest_3h, g = t.greenest_3h, bits = [];
            if (c) bits.push(`cheapest 3h ${c.from_label}–${c.to_label} (${c.avg_p}p)`);
            if (g) bits.push(`greenest 3h ${g.from_label}–${g.to_label} (${Math.round(g.avg_g)} g)`);
            document.getElementById("timeline-best").textContent = bits.join(" · ");
            panel.style.display = "";
        } catch (e) { /* leave hidden */ }
    }

    // ---- insights (baseload, battery economics, capacity) ----
    const insightTiles = {};   // id -> html; rendered together
    function renderInsights() {
        const grid = document.getElementById("insights-grid");
        if (!grid) return;
        const html = Object.values(insightTiles).filter(Boolean).join("");
        grid.innerHTML = html;
        document.getElementById("insights-panel").style.display = html ? "" : "none";
    }
    function tile(label, value, sub, stripe) {
        return `<div class="summary-card"${stripe ? ` style="--stripe:${stripe}"` : ""}>`
            + `<div class="label">${label}</div><div class="value" style="font-size:1.3rem">${value}</div>`
            + `<div class="sub">${sub || ""}</div></div>`;
    }
    async function loadInsights() {
        setTimeout(loadInsights, 1800000);
        try {
            const b = await api("/api/insights/baseload");
            insightTiles.baseload = b.available ? tile("🏠 Always-on draw", esc(b.watts) + " W",
                `≈${Math.round(b.annual_kwh).toLocaleString()} kWh/yr`
                + (b.annual_cost_pence != null ? ` · ≈£${Math.round(b.annual_cost_pence / 100)}/yr` : "")
                + ` · last night ${esc(b.latest_w)} W`, "var(--c-import)") : "";
        } catch (e) { /* optional */ }
        try {
            const e = await api("/api/insights/battery-economics");
            insightTiles.econ = e.available ? tile("🔋 Battery earned",
                fmtMoney(e.this_month_p) + " <small>this month</small>",
                `${fmtMoney(e.all_time_p)} since ${esc(e.since)}`
                + (e.per_kwh_p != null ? ` · ${e.per_kwh_p}p per kWh discharged` : "")
                + (e.cycles != null ? ` · ${e.cycles} cycles` : ""), "var(--c-battery)") : "";
        } catch (e) { /* optional */ }
        try {
            const h = await api("/api/insights/battery-health");
            insightTiles.health = h.available ? tile("🩺 Measured capacity",
                `${h.trend_kwh} kWh`, h.soh_pct != null
                    ? `${h.soh_pct}% of ${h.nameplate_kwh} kWh nameplate` : "set the nameplate in Settings",
                "var(--c-battery)") : "";
        } catch (e) { /* optional */ }
        renderInsights();
    }

    // ---- Octopus account: balance, bill forecast, saving sessions ----
    async function loadAccount() {
        setTimeout(loadAccount, 900000);   // 15 min
        try {
            const a = await api("/api/account");
            const panel = document.getElementById("account-panel");
            if (!panel) return;
            const bf = a.bill_forecast;
            const hasBalance = a.balance_gbp != null;
            if (!hasBalance && !bf) return;   // nothing useful — stay hidden
            if (hasBalance) {
                const el = document.getElementById("acct-balance");
                // Octopus balance: positive = in credit.
                el.textContent = (a.balance_gbp < 0 ? "−£" : "£") + Math.abs(a.balance_gbp).toFixed(2);
                el.style.color = a.balance_gbp < 0 ? "var(--c-peak)" : "var(--c-cheap)";
            }
            if (bf) {
                document.getElementById("acct-mtd").textContent = "£" + (bf.month_to_date_pence / 100).toFixed(2);
                document.getElementById("acct-mtd-sub").textContent =
                    bf.days_with_data + " day" + (bf.days_with_data === 1 ? "" : "s") + " · £" +
                    (bf.avg_daily_pence / 100).toFixed(2) + "/day";
                document.getElementById("acct-proj").textContent = "£" + (bf.projected_month_pence / 100).toFixed(2);
            }
            const ss = a.saving_sessions && a.saving_sessions.next;
            const sEl = document.getElementById("acct-session");
            if (ss && ss.start) {
                const when = new Date(ss.start).toLocaleString([], {
                    weekday: "short", hour: "2-digit", minute: "2-digit" });
                // points_per_kwh is coerced numeric server-side, but this is
                // upstream Kraken data hitting innerHTML — esc() regardless.
                sEl.innerHTML = "🎁 Saving Session <b>" + esc(when) + "</b>" +
                    (ss.points_per_kwh ? " · " + esc(ss.points_per_kwh) + " pts/kWh" : "") +
                    (ss.joined ? " · ✅ joined" : " · ⚠️ <b>not joined — opt in</b>");
                sEl.style.display = "";
            } else {
                sEl.style.display = "none";
            }
            panel.style.display = "";
        } catch (e) { /* leave hidden */ }
    }

    function initDashboard() {
        document.getElementById("nav-prev").addEventListener("click", (e) => {
            if (e.target.dataset.date) loadDay(e.target.dataset.date);
        });
        document.getElementById("nav-next").addEventListener("click", (e) => {
            if (e.target.dataset.date) loadDay(e.target.dataset.date);
        });
        document.getElementById("nav-today").addEventListener("click", () => loadDay(null));
        document.getElementById("nav-date").addEventListener("change", (e) => {
            if (e.target.value) loadDay(e.target.value);
        });
        document.addEventListener("powersoc:theme", applyTheme);
        loadDay(null);
        pollBackfill();
        pollLive();
        loadTariffs();
        loadWeather();
        loadForecast();
        loadPlan();
        loadCarbon();
        loadRoi();
        loadAccount();
        loadInsights();
        loadTimeline();
        pollToday();
        // Catch up immediately when the tab becomes visible again — otherwise
        // a long-idle tab shows stale numbers until the next timer fires.
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) { pollLive(); pollToday(); }
        });
    }

    // ---- history view ----
    const hist = { period: "week", anchor: null, cost: "both", metric: "cost",
                   yoy: false, data: null };

    function localISODate(d) {
        // NEVER toISOString() for calendar dates: it converts to UTC, so
        // during BST every local midnight becomes 23:00Z the previous day —
        // week shifts lost a day and month navigation stuck on the 1st.
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }

    function shiftAnchor(anchor, period, dir) {
        const d = new Date(anchor + "T00:00:00");
        if (period === "week") d.setDate(d.getDate() + 7 * dir);
        else if (period === "month") { d.setDate(1); d.setMonth(d.getMonth() + dir); }
        else d.setFullYear(d.getFullYear() + dir);
        return localISODate(d);
    }

    function fmtMoneyShort(p) {
        return (p == null) ? "–" : (p < 0 ? "−£" : "£") + Math.abs(p / 100).toFixed(2);
    }

    function renderHistory() {
        const p = hist.data;
        if (!p) return;
        document.getElementById("hist-range").textContent =
            p.range.start + "  →  " + p.range.end;

        let datasets, stacked = false, yLabel = "£";
        const pounds = (arr) => arr.map((v) => (v == null ? null : +(v / 100).toFixed(2)));
        const prev = p.compare || {};
        document.getElementById("cost-toggle").style.display = hist.metric === "cost" ? "" : "none";
        if (hist.metric === "energy") {
            datasets = [ds("Solar", p.energy.pv, "solar"), ds("Import", p.energy.import, "import"),
                        ds("Export", p.energy.export, "export")];
            if (p.energy.gas.some((v) => v)) datasets.push(ds("Gas", p.energy.gas, "gas"));
            yLabel = "kWh";
            if (hist.yoy) datasets.push(ds("Solar last year", (prev.energy || {}).pv || [], "solar",
                { type: "line", borderDash: [4, 3], pointRadius: 0 }));
        } else if (hist.metric === "self") {
            datasets = [ds("Self-sufficiency", p.self_sufficiency, "battery")];
            yLabel = "%";
            if (hist.yoy) datasets.push(ds("Last year", prev.self_sufficiency || [], "battery",
                { type: "line", borderDash: [4, 3], pointRadius: 0 }));
        } else {
            if (hist.cost === "elec") {
                datasets = [ds("Electricity", pounds(p.cost.elec), "import")];
            } else if (hist.cost === "gas") {
                datasets = [ds("Gas", pounds(p.cost.gas), "gas")];
            } else {
                datasets = [ds("Electricity", pounds(p.cost.elec), "import"),
                            ds("Gas", pounds(p.cost.gas), "gas")];
                stacked = true;
            }
            if (hist.yoy) {
                const key = hist.cost === "both" ? "net" : hist.cost;
                datasets.push(ds("Last year", pounds((prev.cost || {})[key] || []), "text-muted",
                    { type: "line", borderDash: [4, 3], pointRadius: 0, stack: "prev" }));
            }
        }
        // Mixed bar/line + the YoY toggle change the shape: rebuild, not update.
        if (charts["chart-history"]) { charts["chart-history"].destroy(); delete charts["chart-history"]; }
        makeChart("chart-history", "bar", p.labels, datasets, stacked, yLabel);
        const both = (a, b) => (a == null && b == null) ? null : (a || 0) + (b || 0);
        const t = p.totals, pt = (prev.totals || {});
        const kwh = (v) => (v == null ? "–" : v + " kWh");
        const pctTxt = (v) => (v == null ? "–" : v + "%");
        let totalTxt;
        if (hist.metric === "energy") totalTxt = `${kwh(t.pv_kwh)} solar · ${kwh(t.import_kwh)} imported`;
        else if (hist.metric === "self") totalTxt = `${pctTxt(t.self_sufficiency_pct)} self-sufficient`;
        else totalTxt = fmtMoneyShort(hist.cost === "elec" ? t.elec_cost : hist.cost === "gas"
            ? t.gas_cost : both(t.elec_cost, t.gas_cost)) + " total";
        document.getElementById("hist-total").textContent = totalTxt;

        // Totals cards (+ the change vs the same period last year).
        const vs = (cur, old, money) => {
            if (!hist.yoy || cur == null || old == null || !old) return "";
            const d = Math.round((cur - old) / Math.abs(old) * 100);
            return `<div class="sub">${d >= 0 ? "▲" : "▼"} ${Math.abs(d)}% vs last year `
                + `(${money ? fmtMoneyShort(old) : old})</div>`;
        };
        document.getElementById("hist-totals").innerHTML = [
            ["Net cost", fmtMoneyShort(t.net_cost), vs(t.net_cost, pt.net_cost, true)],
            ["Saved vs Flexible", t.saving_pence != null ? fmtMoneyShort(t.saving_pence) : "–", ""],
            ["Solar", kwh(t.pv_kwh), vs(t.pv_kwh, pt.pv_kwh)],
            ["Import", kwh(t.import_kwh), vs(t.import_kwh, pt.import_kwh)],
            ["Export", kwh(t.export_kwh), vs(t.export_kwh, pt.export_kwh)],
            ["Self-sufficiency", pctTxt(t.self_sufficiency_pct), vs(t.self_sufficiency_pct, pt.self_sufficiency_pct)],
            ["Gas", kwh(t.gas_kwh), vs(t.gas_kwh, pt.gas_kwh)],
        ].map(([l, v, sub]) =>
            `<div class="card summary-card"><div class="label">${l}</div>`
            + `<div class="value" style="font-size:1.3rem">${v}</div>${sub}</div>`).join("");

        // Table.
        const tb = document.querySelector("#hist-table tbody");
        const n = (v) => (v == null ? "–" : v);
        tb.innerHTML = p.table.map((r) =>
            `<tr><td>${r.label}</td><td>${fmtMoneyShort(r.elec_cost)}</td>`
            + `<td>${fmtMoneyShort(r.gas_cost)}</td><td>${fmtMoneyShort(r.net_cost)}</td>`
            + `<td>${n(r.pv_kwh)}</td><td>${n(r.import_kwh)}</td><td>${n(r.export_kwh)}</td></tr>`
        ).join("");
    }

    let histSeq = 0;   // a slow older response must never overwrite a newer one
    async function loadHistory() {
        const my = ++histSeq;
        try {
            const d = await api(`/api/history?period=${hist.period}&anchor=${hist.anchor}`);
            if (my !== histSeq) return;
            hist.data = d;
            renderHistory();
            loadHeatmap();
        } catch (e) { console.error("loadHistory failed", e); }
    }

    // Calendar heatmap for the year view (quick win 6).
    let heatYear = null, heatRows = null;
    async function loadHeatmap() {
        const card = document.getElementById("heatmap-card");
        if (!card) return;
        if (hist.period !== "year") { card.style.display = "none"; return; }
        const year = Number(hist.anchor.slice(0, 4));
        if (year !== heatYear) {
            try { heatRows = await api("/api/history/calendar?year=" + year); heatYear = year; }
            catch (e) { return; }
        }
        const metric = document.getElementById("heatmap-metric").value;
        const vals = heatRows.map((r) => r[metric]).filter((v) => v != null);
        const lo = Math.min(...vals), hi = Math.max(...vals);
        // Greener = better: low cost/import, high solar/self-sufficiency.
        const goodHigh = metric === "pv_kwh" || metric === "self_sufficiency";
        const fmt = (v) => metric === "net_cost" ? fmtMoneyShort(v)
            : metric === "self_sufficiency" ? v + "%" : (+v).toFixed(1) + " kWh";
        const first = new Date(heatRows[0].date + "T00:00:00");
        const pad = (first.getDay() + 6) % 7;     // Monday-first weeks
        const cells = Array(pad).fill('<span class="hm hm-blank"></span>');
        heatRows.forEach((r) => {
            const v = r[metric];
            if (v == null) { cells.push(`<span class="hm" title="${r.date}: no data"></span>`); return; }
            let f = hi > lo ? (v - lo) / (hi - lo) : 0.5;
            if (!goodHigh) f = 1 - f;
            // A flat range (every day the same) is neutral, not "bad".
            const col = hi - lo < 1e-9 ? alpha(cssVar("--accent"), 0.5)
                : f > 0.5 ? alpha(cssVar("--c-cheap"), 0.25 + (f - 0.5) * 1.5)
                          : alpha(cssVar("--c-peak"), 0.25 + (0.5 - f) * 1.5);
            cells.push(`<span class="hm" style="background:${col}" title="${r.date}: ${esc(fmt(v))}"></span>`);
        });
        document.getElementById("heatmap").innerHTML = cells.join("");
        document.getElementById("heatmap-year").textContent = year;
        document.getElementById("heatmap-legend").textContent = vals.length
            ? `Range ${fmt(lo)} – ${fmt(hi)} · green = better · columns are weeks (Mon at top)` : "No data this year.";
        card.style.display = "";
    }

    async function loadTariffSim() {
        try {
            const s = await api("/api/tariff-sim");
            const note = document.getElementById("tariffsim-note");
            if (!note) return;
            if (s.running) { setTimeout(loadTariffSim, 2000); return; }
            const r = s.result;
            if (!r) return;
            if (r.error) { note.textContent = "Couldn't compare: " + r.error; return; }
            note.textContent = `${r.from} → ${r.to} (${r.days} days, ${r.kwh} kWh imported). `
                + `You paid ${fmtMoneyShort(r.actual.total_p)} for electricity import + standing. `
                + r.note + ` Compared ${new Date(r.ran_at).toLocaleString()}.`;
            const tb = document.querySelector("#tariffsim-table tbody");
            tb.innerHTML = r.tariffs.map((t) =>
                `<tr><td>${esc(t.name)} <small class="muted">${esc(t.code)}</small></td>`
                + `<td>${fmtMoneyShort(t.import_p)}</td><td>${fmtMoneyShort(t.standing_p)}</td>`
                + `<td>${fmtMoneyShort(t.total_p)}</td>`
                + `<td style="color:${t.delta_p < 0 ? "var(--c-cheap)" : "var(--c-peak)"}">`
                + `${t.delta_p < 0 ? "−" : "+"}${fmtMoneyShort(Math.abs(t.delta_p)).replace("−", "")}`
                + `${t.coverage != null && t.coverage < 0.98 ? ` <small>(${Math.round(t.coverage * 100)}% priced)</small>` : ""}</td></tr>`
            ).join("");
            document.getElementById("tariffsim-table").style.display = "";
        } catch (e) { /* optional */ }
    }

    async function loadReconcile() {
        try {
            const d = await api("/api/reconcile");
            const card = document.getElementById("reconcile-card");
            if (!card) return;
            // With Octopus statements: one row per BILL, over the bill's own
            // period. Without: our cost per calendar month.
            const rows = (d.statements && d.statements.length)
                ? d.statements.map((r) => ({ label: `${r.from} → ${r.to}`, ...r,
                                             days_label: `${r.days_with_data}/${r.days}` }))
                : d.months.map((r) => ({ label: r.month, ...r, days_label: r.days_with_data }));
            document.querySelector("#reconcile-table tbody").innerHTML = rows.map((r) =>
                `<tr><td>${esc(r.label)}</td><td>${fmtMoneyShort(r.our_cost_pence)}</td>`
                + `<td>${r.billed_pence != null ? fmtMoneyShort(r.billed_pence) : "–"}</td>`
                + `<td>${r.delta_pence != null ? fmtMoneyShort(r.delta_pence) : "–"}</td>`
                + `<td>${esc(r.days_label)}</td></tr>`).join("");
            const note = document.getElementById("reconcile-note");
            if (note) note.textContent = d.billed_available
                ? "Each Octopus bill against our computed net cost for the same period."
                : "Our computed net cost per month — cross-check against your Octopus bill (live statement totals unavailable).";
            card.style.display = "";
        } catch (e) { /* leave hidden */ }
    }

    function initHistory() {
        hist.anchor = localISODate(new Date());
        // Optional jump-to-date input (sidesteps repeated prev/next stepping).
        const jump = document.getElementById("hist-date");
        if (jump) jump.addEventListener("change", (e) => {
            if (e.target.value) { hist.anchor = e.target.value; loadHistory(); }
        });
        document.querySelectorAll("#period-tabs button").forEach((b) =>
            b.addEventListener("click", () => {
                document.querySelectorAll("#period-tabs button")
                    .forEach((x) => x.classList.remove("active"));
                b.classList.add("active");
                hist.period = b.dataset.period;
                loadHistory();
            }));
        document.querySelectorAll("#cost-toggle button").forEach((b) =>
            b.addEventListener("click", () => {
                document.querySelectorAll("#cost-toggle button")
                    .forEach((x) => x.classList.remove("active"));
                b.classList.add("active");
                hist.cost = b.dataset.cost;
                renderHistory();
            }));
        document.querySelectorAll("#metric-toggle button").forEach((b) =>
            b.addEventListener("click", () => {
                document.querySelectorAll("#metric-toggle button")
                    .forEach((x) => x.classList.remove("active"));
                b.classList.add("active");
                hist.metric = b.dataset.metric;
                renderHistory();
            }));
        const yoy = document.getElementById("hist-yoy");
        if (yoy) yoy.addEventListener("change", () => { hist.yoy = yoy.checked; renderHistory(); });
        const hm = document.getElementById("heatmap-metric");
        if (hm) hm.addEventListener("change", loadHeatmap);
        document.getElementById("hist-prev").addEventListener("click", () => {
            hist.anchor = shiftAnchor(hist.anchor, hist.period, -1); loadHistory();
        });
        document.getElementById("hist-next").addEventListener("click", () => {
            hist.anchor = shiftAnchor(hist.anchor, hist.period, +1); loadHistory();
        });
        document.addEventListener("powersoc:theme", () => { applyTheme(); });
        loadHistory();
        loadReconcile();
        loadTariffSim();
        const simBtn = document.getElementById("btn-tariffsim");
        if (simBtn) simBtn.addEventListener("click", async () => {
            try {
                await post("/api/tariff-sim/run",
                           { days: Number(document.getElementById("tariffsim-days").value) });
                document.getElementById("tariffsim-note").textContent = "Comparing… (a few seconds)";
                setTimeout(loadTariffSim, 2000);
            } catch (e) { document.getElementById("tariffsim-note").textContent = e.message; }
        });
    }

    // ---- settings view ----
    async function post(path, body) {
        const r = await fetch(path, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
        });
        const j = await r.json();
        if (!j.success) throw new Error(j.error || "request failed");
        return j.data;
    }

    function collectCreds() {
        const out = {};
        document.querySelectorAll("[data-cred]").forEach((el) => {
            const v = el.value.trim();
            if (v !== "") out[el.dataset.cred] = v;
            // A non-secret field that HAD a value and is now blank is a
            // deliberate clear (blank used to mean "unchanged", so e.g. the
            // Mattermost channel could never be removed). Secrets are never
            // echoed back, so for them blank still means "keep".
            else if (el.dataset.loaded) out[el.dataset.cred] = null;
        });
        return out;
    }

    // Test/discover requests want the values typed, not clear-markers.
    function typedCreds() {
        const out = collectCreds();
        Object.keys(out).forEach((k) => { if (out[k] == null) delete out[k]; });
        return out;
    }

    function flash(id, msg, ok) {
        const el = document.getElementById(id);
        if (el) { el.textContent = msg; el.style.color = ok ? "var(--accent-2)" : "var(--danger)"; }
    }

    async function loadUsers() {
        const users = await api("/api/users");
        const tb = document.querySelector("#users-table tbody");
        tb.innerHTML = users.map((u) =>
            `<tr><td>${esc(u.username)}</td><td>${esc(u.role)}</td>`
            + `<td>${u.last_login_at ? esc(u.last_login_at) : "–"}</td>`
            + `<td>${u.disabled ? "disabled" : "active"}</td>`
            + `<td><button class="btn" data-act="pw" data-id="${u.id}">Reset pw</button> `
            + `<button class="btn" data-act="revoke" data-id="${u.id}">Revoke sessions</button> `
            + `<button class="btn" data-act="dis" data-id="${u.id}">${u.disabled ? "Enable" : "Disable"}</button> `
            + `<button class="btn" data-act="del" data-id="${u.id}">Delete</button></td></tr>`
        ).join("");
        tb.querySelectorAll("button[data-act]").forEach((b) =>
            b.addEventListener("click", () => userAction(b.dataset.act, b.dataset.id, b)));
    }

    function inlinePasswordReset(id, btn) {
        // Masked inline input instead of prompt() — prompt shows the new
        // password in cleartext on screen.
        const cell = btn.parentElement;
        if (cell.querySelector("input")) return;
        const inp = document.createElement("input");
        inp.type = "password";
        inp.placeholder = "new password (min 8)";
        inp.autocomplete = "new-password";
        inp.style.width = "12em";
        const okBtn = document.createElement("button");
        okBtn.className = "btn";
        okBtn.textContent = "Set";
        const cleanup = () => { inp.remove(); okBtn.remove(); };
        okBtn.addEventListener("click", async () => {
            try {
                if ((inp.value || "").length < 8) {
                    flash("user-result", "password must be ≥ 8 chars", false);
                    return;
                }
                await post(`/api/users/${id}/password`, { password: inp.value });
                flash("user-result", "password reset · sessions revoked", true);
                cleanup();
                loadUsers();
            } catch (e) { flash("user-result", e.message, false); }
        });
        cell.appendChild(inp);
        cell.appendChild(okBtn);
        inp.focus();
    }

    async function userAction(act, id, btn) {
        try {
            if (act === "pw") {
                inlinePasswordReset(id, btn);
                return;
            } else if (act === "dis") {
                const row = (await api("/api/users")).find((u) => u.id == id);
                await post(`/api/users/${id}/disable`, { disabled: !row.disabled });
            } else if (act === "revoke") {
                await post(`/api/users/${id}/revoke-sessions`, {});
                flash("user-result", "sessions revoked", true);
            } else if (act === "del") {
                if (!confirm("Delete this user?")) return;
                const r = await fetch(`/api/users/${id}`, { method: "DELETE" });
                const j = await r.json();
                // A refused delete (e.g. last admin) used to look like success.
                if (!j.success) throw new Error(j.error || "delete failed");
            }
            loadUsers();
        } catch (e) { flash("user-result", e.message, false); }
    }

    let bfTimer = null;   // one polling loop, however many times it's kicked
    async function loadBackfillStatus() {
        const el = document.getElementById("backfill-coverage");
        if (!el) return;
        if (bfTimer) { clearTimeout(bfTimer); bfTimer = null; }
        try {
            const d = await api("/api/backfill/status");
            const c = d.coverage || {};
            let txt = c.earliest
                ? `Stored history: <strong>${c.days_stored}</strong> days, ${c.earliest} → ${c.latest}.`
                : "No history stored yet.";
            if (d.running && d.mode === "reprice") {
                txt += ` <span style="color:var(--accent)">Re-pricing history…</span>`;
            } else if (d.running) {
                txt += ` <span style="color:var(--accent)">Backfilling… ${d.days_done}/${d.days_total}`
                    + (d.skipped ? ` (${d.skipped} already complete)` : "") + "</span>";
            } else if (d.error) {
                txt += ` <span style="color:var(--danger)">Last run: ${esc(d.error)}</span>`;
            } else if (d.repriced && d.repriced.intervals) {
                txt += ` Last re-price: ${d.repriced.intervals} half-hours across `
                    + `${d.repriced.days} days.`;
            }
            el.innerHTML = txt;
            // Always reschedule (slow when idle) so a backfill started from
            // another tab still shows progress here.
            bfTimer = setTimeout(loadBackfillStatus, d.running ? 3000 : 60000);
        } catch (e) {
            el.textContent = "";
            bfTimer = setTimeout(loadBackfillStatus, 60000);
        }
    }

    // Pair bare <label>text</label><input> rows with for/id so screen readers
    // announce the field and clicking the label focuses it.
    function pairLabels() {
        let i = 0;
        document.querySelectorAll(".form-row > label").forEach((lab) => {
            const inp = lab.nextElementSibling;
            if (!inp || !/^(INPUT|SELECT|TEXTAREA)$/.test(inp.tagName)
                || lab.htmlFor) return;
            if (!inp.id) inp.id = "f-auto-" + (i++);
            lab.htmlFor = inp.id;
        });
    }

    async function initSettings() {
        pairLabels();
        if (new URLSearchParams(location.search).get("setup")) {
            document.getElementById("setup-banner").style.display = "";
        }
        // Populate non-secret fields; mark secrets as set with a placeholder.
        // A stored value that no longer decrypts (key changed, e.g. after a
        // restore) reads as unset — flag it loudly rather than show a blank.
        async function loadCredState() {
            try {
                const s = await api("/api/settings");
                const bad = [];
                for (const [key, meta] of Object.entries(s.credentials)) {
                    const el = document.querySelector(`[data-cred="${key}"]`);
                    if (!el) continue;
                    if (el.dataset.ph0 === undefined) el.dataset.ph0 = el.placeholder;
                    el.placeholder = el.dataset.ph0;
                    el.classList.toggle("cred-unreadable", !!meta.unreadable);
                    if (meta.unreadable) {
                        el.placeholder = "⚠ saved value can't be decrypted — re-enter (blank clears it)";
                        const lab = el.closest(".form-row")?.querySelector("label");
                        bad.push(lab ? lab.textContent.trim() : key);
                    } else if (meta.secret) {
                        if (meta.set) el.placeholder = "•••••••• (set — leave blank to keep)";
                    } else if (meta.value) {
                        el.value = meta.value;
                        el.dataset.loaded = "1";
                    } else {
                        delete el.dataset.loaded;
                    }
                }
                const warn = document.getElementById("cred-warning");
                warn.textContent = bad.length
                    ? `⚠️ ${bad.length} saved credential${bad.length > 1 ? "s" : ""} can't be ` +
                      "decrypted — the encryption key has changed (e.g. after a restore), so " +
                      "PowerSOC treats them as unset: " + bad.join(", ") + ". Re-enter the " +
                      "highlighted fields and click Save credentials — any left blank are cleared."
                    : "";
                warn.style.display = bad.length ? "" : "none";
            } catch (e) { /* not logged in / error */ }
        }
        await loadCredState();

        document.getElementById("btn-save").addEventListener("click", async () => {
            try {
                const body = collectCreds();
                // Unreadable values are useless — a blank field clears them
                // (otherwise blank means "unchanged" and the warning sticks).
                document.querySelectorAll("[data-cred].cred-unreadable").forEach((el) => {
                    if (!(el.dataset.cred in body)) body[el.dataset.cred] = null;
                });
                const d = await post("/api/settings/credentials", body);
                flash("settings-status", "Saved: " + (d.changed.join(", ") || "no changes"), true);
                await loadCredState();
            } catch (e) { flash("settings-status", e.message, false); }
        });
        document.getElementById("btn-test-sunsynk").addEventListener("click", async () => {
            flash("sunsynk-result", "testing…", true);
            try {
                const d = await post("/api/settings/test/sunsynk", typedCreds());
                flash("sunsynk-result", "OK — plants: "
                    + d.plants.map((p) => p.name).join(", "), true);
            } catch (e) { flash("sunsynk-result", e.message, false); }
        });
        document.getElementById("btn-discover").addEventListener("click", async () => {
            flash("discover-result", "discovering…", true);
            try {
                const m = await post("/api/settings/discover", typedCreds());
                const set = (sel, v) => { const el = document.querySelector(sel); if (el && v) el.value = v; };
                set('[data-cred="OCTOPUS_MPAN"]', m.import_mpan);
                set('[data-cred="OCTOPUS_METER_SERIAL"]', m.import_serial);
                set('[data-cred="OCTOPUS_EXPORT_MPAN"]', m.export_mpan);
                set('[data-cred="OCTOPUS_EXPORT_SERIAL"]', m.export_serial);
                set('[data-cred="OCTOPUS_MPRN"]', m.mprn);
                set('[data-cred="OCTOPUS_GAS_SERIAL"]', m.gas_serial);
                flash("discover-result", "Discovered region " + (m.region || "?")
                    + " — review and Save.", true);
            } catch (e) { flash("discover-result", e.message, false); }
        });
        document.getElementById("btn-test-webhook").addEventListener("click", async () => {
            try {
                const d = await post("/api/settings/test/webhook", typedCreds());
                flash("webhook-result", d.sent ? "Sent ✓" : ("Failed: " + d.detail), d.sent);
            } catch (e) { flash("webhook-result", e.message, false); }
        });
        document.getElementById("btn-test-brief").addEventListener("click", async () => {
            try {
                const d = await post("/api/brief/test", typedCreds());
                flash("webhook-result", d.sent ? "Brief sent ✓" : ("Not sent: " + d.detail), d.sent);
            } catch (e) { flash("webhook-result", e.message, false); }
        });
        async function startBackfill(force) {
            try {
                const d = await post("/api/backfill/start", { force: force });
                flash("backfill-result", d.started
                    ? (force ? "Full re-backfill started" : "Backfill started")
                    : "Already running", true);
                loadBackfillStatus();
            } catch (e) { flash("backfill-result", e.message, false); }
        }
        document.getElementById("btn-backfill").addEventListener("click", () => startBackfill(false));
        document.getElementById("btn-backfill-force").addEventListener("click", () => {
            if (confirm("Re-fetch every source for every day from the APIs, even complete ones? "
                        + "To fix prices only, Re-price history is much lighter."))
                startBackfill(true);
        });
        document.getElementById("btn-reprice").addEventListener("click", async () => {
            try {
                const d = await post("/api/backfill/reprice", {});
                flash("backfill-result", d.started ? "Re-price started" : "Already running", true);
                loadBackfillStatus();
            } catch (e) { flash("backfill-result", e.message, false); }
        });
        const themeName = () => {
            const el = document.getElementById("theme-name");
            if (el) el.textContent = "Current: " + window.PowerSOCTheme.current()
                + " (" + window.PowerSOCTheme.themes.join(" → ") + ")";
        };
        themeName();
        document.getElementById("btn-theme").addEventListener("click", () => {
            window.PowerSOCTheme.toggle();
            themeName();
        });
        document.getElementById("btn-add-user").addEventListener("click", async () => {
            try {
                await post("/api/users", {
                    username: document.getElementById("new-username").value,
                    password: document.getElementById("new-password").value });
                document.getElementById("new-username").value = "";
                document.getElementById("new-password").value = "";
                flash("user-result", "Added ✓", true);
                loadUsers();
            } catch (e) { flash("user-result", e.message, false); }
        });
        // Home API: one token per consumer (O1).
        const ago = (iso) => {
            if (!iso) return "never";
            const m = Math.round((Date.now() - new Date(iso)) / 60000);
            return m < 1 ? "just now" : m < 120 ? m + " min ago"
                : m < 2880 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " days ago";
        };
        async function loadConsumers() {
            try {
                const d = await api("/api/settings/home-consumers");
                const tb = document.querySelector("#home-consumers tbody");
                tb.innerHTML = d.consumers.length ? d.consumers.map((c) =>
                    `<tr><td>${esc(c.name)}</td><td>${esc(ago(c.last_seen_at))}</td>`
                    + `<td>${esc(c.last_ip || "–")}</td><td>${Number(c.requests || 0)}</td>`
                    + `<td><button class="btn" data-revoke="${esc(c.id)}">Revoke</button></td></tr>`).join("")
                    : `<tr><td colspan="5" class="muted">No tokens — the home API is off.</td></tr>`;
                tb.querySelectorAll("button[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
                    if (!confirm("Revoke this token? The consumer stops working until it gets a new one.")) return;
                    try {
                        const r = await fetch("/api/settings/home-consumers/" + encodeURIComponent(b.dataset.revoke),
                                              { method: "DELETE" });
                        const j = await r.json();
                        if (!j.success) throw new Error(j.error || "revoke failed");
                        flash("token-result", "Revoked", true);
                        loadConsumers();
                    } catch (e) { flash("token-result", e.message, false); }
                }));
                const rej = document.getElementById("home-rejects");
                rej.style.display = d.rejections.length ? "" : "none";
                document.querySelector("#home-rejects-table tbody").innerHTML = d.rejections.map((r) =>
                    `<tr><td><code>…${esc(r.fingerprint)}</code></td><td>${esc(r.last_ip || "–")}</td>`
                    + `<td>${Number(r.count_today)}</td><td>${Number(r.total)}</td>`
                    + `<td>${esc(ago(r.last_at))} · ${esc(r.last_path || "")}</td></tr>`).join("");
            } catch (e) { /* not admin */ }
        }
        document.getElementById("btn-consumer-add").addEventListener("click", async () => {
            const name = document.getElementById("home-consumer-name").value.trim();
            try {
                const d = await post("/api/settings/home-consumers", { name: name });
                const box = document.getElementById("home-token-value");
                box.style.display = "";
                box.innerHTML = `Token for <strong>${esc(d.name)}</strong> (copy now — shown once; `
                    + `fingerprint …${esc(d.fingerprint)}):<br><code>${esc(d.token)}</code>`;
                document.getElementById("home-consumer-name").value = "";
                loadConsumers();
            } catch (e) { flash("token-result", e.message, false); }
        });

        // System & battery (ROI + SoH inputs) + gas units + location
        try {
            const sysd = await api("/api/settings/system");
            const set = (id, v) => { const el = document.getElementById(id); if (el && v != null && v !== "") el.value = v; };
            set("sys-cost", sysd.system_cost_gbp);
            set("sys-nameplate", sysd.battery_nameplate_kwh);
            set("sys-cycles", sysd.battery_rated_cycles);
            set("sys-reserve", sysd.battery_reserve_pct);
            set("sys-charge-kw", sysd.battery_charge_kw);
            set("sys-gas-units", sysd.gas_units || "m3");
            set("sys-wx-lat", sysd.weather_lat);
            set("sys-wx-lon", sysd.weather_lon);
            set("sys-wx-name", sysd.weather_name);
            set("sys-outcode", sysd.carbon_outcode);
        } catch (e) { /* ignore */ }
        document.getElementById("btn-save-system").addEventListener("click", async () => {
            // An invalid entry must be REFUSED, never sent as null: "1e999"
            // parsed to Infinity, JSON turned that into null, the server read
            // null as "clear" — the setting was erased and the UI said
            // "Saved ✓" (2026-10-07 rule, implemented 2026-10-26).
            const bad = [];
            const num = (id) => {
                const el = document.getElementById(id);
                const raw = el.value.trim();
                if (el.validity && el.validity.badInput) { bad.push(el); return undefined; }
                if (raw === "") return null;
                const v = Number(raw);
                if (!Number.isFinite(v) || v < 0) { bad.push(el); return undefined; }
                return v;
            };
            const txt = (id) => { const el = document.getElementById(id); return el ? el.value.trim() : ""; };
            const nums = { system_cost_gbp: num("sys-cost"),
                           battery_nameplate_kwh: num("sys-nameplate"),
                           battery_rated_cycles: num("sys-cycles"),
                           battery_reserve_pct: num("sys-reserve"),
                           battery_charge_kw: num("sys-charge-kw") };
            document.querySelectorAll(".input-invalid").forEach((el) => el.classList.remove("input-invalid"));
            if (bad.length) {
                bad.forEach((el) => el.classList.add("input-invalid"));
                flash("system-result", "Not saved — enter a valid, finite, non-negative number", false);
                bad[0].focus();
                return;
            }
            try {
                await post("/api/settings/system", {
                    ...nums,
                    gas_units: txt("sys-gas-units") || "m3",
                    weather_lat: txt("sys-wx-lat"),
                    weather_lon: txt("sys-wx-lon"),
                    weather_name: txt("sys-wx-name"),
                    carbon_outcode: txt("sys-outcode"),
                });
                flash("system-result", "Saved ✓", true);
            } catch (e) { flash("system-result", e.message, false); }
        });

        // Security: 2FA + session revocation (current account).
        async function load2fa() {
            try {
                const d = await api("/api/2fa/status");
                document.getElementById("twofa-status").textContent =
                    d.enabled ? "Two-factor auth: enabled ✓" : "Two-factor auth: disabled.";
                document.getElementById("btn-2fa-enable").style.display = d.enabled ? "none" : "";
                document.getElementById("btn-2fa-disable").style.display = d.enabled ? "" : "none";
                document.getElementById("btn-2fa-recovery").style.display = d.enabled ? "" : "none";
            } catch (e) { /* ignore */ }
        }
        document.getElementById("btn-2fa-enable").addEventListener("click", async () => {
            try {
                const d = await post("/api/2fa/setup", {});
                document.getElementById("twofa-secret").innerHTML =
                    "Secret: <code>" + esc(d.secret) + "</code><br><small>" + esc(d.otpauth_uri) + "</small>";
                document.getElementById("twofa-setup").style.display = "";
            } catch (e) { flash("twofa-result", e.message, false); }
        });
        function showRecoveryCodes(codes) {
            document.getElementById("twofa-secret").innerHTML =
                "<strong>Recovery codes — save these now (shown once):</strong><br><code>"
                + codes.map(esc).join("</code> · <code>")
                + "</code><br><small>Each works once in the login page's 2FA field if you lose your authenticator.</small>";
            document.getElementById("twofa-setup").style.display = "";
            document.getElementById("twofa-code").parentElement.style.display = "none";
        }
        document.getElementById("btn-2fa-recovery").addEventListener("click", async () => {
            const code = prompt("Enter a current authenticator code to replace your recovery codes:");
            if (!code) return;
            try {
                const d = await post("/api/2fa/recovery-codes", { code: code });
                showRecoveryCodes(d.recovery_codes);
                flash("twofa-result", "New recovery codes issued — the old ones no longer work", true);
            } catch (e) { flash("twofa-result", e.message, false); }
        });
        document.getElementById("btn-2fa-confirm").addEventListener("click", async () => {
            try {
                const d = await post("/api/2fa/enable",
                    { code: document.getElementById("twofa-code").value });
                flash("twofa-result", "2FA enabled ✓", true);
                // Recovery codes are shown exactly once — make that obvious.
                if (d && d.recovery_codes) {
                    showRecoveryCodes(d.recovery_codes);
                } else {
                    document.getElementById("twofa-setup").style.display = "none";
                }
                load2fa();
            } catch (e) { flash("twofa-result", e.message, false); }
        });
        document.getElementById("btn-2fa-disable").addEventListener("click", async () => {
            const code = prompt("Enter a current authenticator code to disable 2FA:");
            if (!code) return;
            try { await post("/api/2fa/disable", { code: code }); flash("twofa-result", "2FA disabled", true); load2fa(); }
            catch (e) { flash("twofa-result", e.message, false); }
        });
        document.getElementById("btn-revoke-sessions").addEventListener("click", async () => {
            if (!confirm("Sign out all other sessions/devices?")) return;
            try { await post("/api/sessions/revoke", {}); flash("twofa-result", "Other sessions signed out ✓", true); }
            catch (e) { flash("twofa-result", e.message, false); }
        });
        load2fa();

        // Daily brief receipt + resend (O2) and instance role (O3).
        async function loadBriefStatus() {
            const el = document.getElementById("brief-status");
            try {
                const b = await api("/api/brief/status");
                const a = b.last_attempt;
                let txt = b.ok === null
                    ? (b.role === "standby" ? "This instance is a standby — the primary posts the brief."
                                            : "No webhook configured — the brief isn't sent.")
                    : b.ok ? "✅ Up to date" : "⚠️ Yesterday's brief has NOT been delivered";
                txt += ` · last delivered: <strong>${esc(b.last_delivered || "never")}</strong>`;
                if (a) txt += `<br>Last attempt: ${esc(a.date)} via ${esc(a.via)} at `
                    + `${esc((a.at || "").replace("T", " ").slice(0, 16))} — `
                    + (a.ok ? "delivered" : `<span style="color:var(--danger)">failed`
                       + `${a.attempts > 1 ? ` (${a.attempts} tries)` : ""}: ${esc(a.detail)}</span>`);
                el.innerHTML = txt;
                const d = document.getElementById("brief-date");
                if (d && !d.value) d.value = b.due_date;
            } catch (e) { el.textContent = ""; }
        }
        document.getElementById("btn-brief-resend").addEventListener("click", async () => {
            flash("brief-result", "sending…", true);
            try {
                const d = await post("/api/brief/resend",
                    { date: document.getElementById("brief-date").value || null });
                flash("brief-result", d.sent ? "Delivered ✓" : "Not delivered: " + d.detail, d.sent);
            } catch (e) { flash("brief-result", e.message, false); }
            loadBriefStatus();
        });
        try {
            const inst = await api("/api/settings/instance");
            document.getElementById("inst-name").value = inst.instance_name || "";
            document.getElementById("inst-role").value = inst.instance_role || "primary";
            document.getElementById("inst-peer").value = inst.peer_url || "";
        } catch (e) { /* ignore */ }
        document.getElementById("btn-inst-save").addEventListener("click", async () => {
            try {
                await post("/api/settings/instance", {
                    instance_name: document.getElementById("inst-name").value.trim(),
                    instance_role: document.getElementById("inst-role").value,
                    peer_url: document.getElementById("inst-peer").value.trim() });
                flash("inst-result", "Saved ✓", true);
                loadBriefStatus();
            } catch (e) { flash("inst-result", e.message, false); }
        });
        loadBriefStatus();

        // Alerts & monitoring (quick win 3).
        async function loadAlerts() {
            try {
                const a = await api("/api/settings/alerts");
                document.getElementById("al-enabled").checked = !!a.enabled;
                document.getElementById("al-low").value = a.battery_low_pct ?? "";
                document.getElementById("al-cost").value = a.high_cost_gbp ?? "";
                document.getElementById("al-peak").value = a.peak_kwh ?? "";
                document.getElementById("al-quiet").value = a.quiet_hours || "";
                document.getElementById("al-hb").placeholder = a.heartbeat_set
                    ? "•••••••• (set — leave blank to keep)" : "https://hc-ping.com/<uuid>";
            } catch (e) { /* not admin */ }
        }
        document.getElementById("btn-alerts-save").addEventListener("click", async () => {
            const bad = [];
            const num = (id) => {
                const el = document.getElementById(id), raw = el.value.trim();
                if (el.validity && el.validity.badInput) { bad.push(el); return undefined; }
                if (raw === "") return null;
                const v = Number(raw);
                if (!Number.isFinite(v)) { bad.push(el); return undefined; }
                return v;
            };
            const body = { enabled: document.getElementById("al-enabled").checked,
                           battery_low_pct: num("al-low"), high_cost_gbp: num("al-cost"),
                           peak_kwh: num("al-peak"),
                           quiet_hours: document.getElementById("al-quiet").value.trim() };
            const hb = document.getElementById("al-hb").value.trim();
            if (hb) body.heartbeat_url = hb;
            if (bad.length) { bad[0].focus(); flash("alerts-result", "Not saved — enter valid numbers", false); return; }
            try {
                await post("/api/settings/alerts", body);
                document.getElementById("al-hb").value = "";
                flash("alerts-result", "Saved ✓", true);
                loadAlerts();
            } catch (e) { flash("alerts-result", e.message, false); }
        });
        document.getElementById("btn-hb-test").addEventListener("click", async () => {
            try {
                const d = await post("/api/settings/alerts/test-heartbeat", {});
                flash("alerts-result", d.ok ? "Heartbeat ping OK ✓"
                    : "Ping failed" + (d.status ? " (HTTP " + d.status + ")" : d.error ? " (" + d.error + ")" : ""), d.ok);
            } catch (e) { flash("alerts-result", e.message, false); }
        });
        loadAlerts();

        loadUsers();
        loadBackfillStatus();
        loadConsumers();
    }

    // ---- first-run setup wizard ----
    function collectStepCreds(stepEl) {
        const out = {};
        stepEl.querySelectorAll("[data-cred]").forEach((el) => {
            const v = el.value.trim();
            if (v !== "") out[el.dataset.cred] = v;
        });
        return out;
    }

    function initSetup() {
        pairLabels();
        const TOTAL = 5;
        const OPTIONAL = { 2: true, 3: true, 4: true };
        let step = 1;
        let accountCreated = false;   // Back to step 1 must not re-POST /setup
        const errBox = document.getElementById("wizard-error");
        const backBtn = document.getElementById("wizard-back");
        const skipBtn = document.getElementById("wizard-skip");
        const nextBtn = document.getElementById("wizard-next");

        function showError(msg) {
            if (!msg) { errBox.style.display = "none"; return; }
            errBox.textContent = msg; errBox.style.display = "";
        }
        function progress() {
            const dots = [];
            for (let i = 1; i <= TOTAL; i++) {
                dots.push(`<span class="wp-dot${i === step ? " active" : (i < step ? " done" : "")}"></span>`);
            }
            document.getElementById("wizard-progress").innerHTML =
                `<span class="muted">Step ${step} of ${TOTAL}</span> ` + dots.join("");
        }
        function stepEl(n) { return document.querySelector(`.wizard-step[data-step="${n}"]`); }

        function buildReview() {
            const has = (k) => {
                const el = document.querySelector(`[data-cred="${k}"]`);
                return el && el.value.trim() !== "";
            };
            const line = (label, on) =>
                `${on ? "✓" : "—"} ${label}: <strong>${on ? "configured" : "skipped"}</strong>`;
            document.getElementById("su-review").innerHTML = [
                line("Sunsynk", has("SUNSYNK_USERNAME") && has("SUNSYNK_PASSWORD")),
                line("Octopus", has("OCTOPUS_API_KEY") && has("OCTOPUS_ACCOUNT_NUMBER")),
                line("Daily brief", has("MATTERMOST_WEBHOOK_URL")),
            ].join("<br>");
        }

        function show() {
            showError("");
            for (let i = 1; i <= TOTAL; i++) {
                const el = stepEl(i);
                if (el) el.style.display = i === step ? "" : "none";
            }
            backBtn.style.display = step > 1 ? "" : "none";
            skipBtn.style.display = OPTIONAL[step] ? "" : "none";
            nextBtn.textContent = step === 1 ? (accountCreated ? "Continue" : "Create account")
                : step === TOTAL ? "Start & finish" : "Save & continue";
            if (step === TOTAL) buildReview();
            progress();
        }

        async function advance() {
            try {
                if (step === 1 && accountCreated) {
                    /* already created — just move on */
                } else if (step === 1) {
                    const setup_token = document.getElementById("su-token").value.trim();
                    const username = document.getElementById("su-username").value.trim();
                    const password = document.getElementById("su-password").value;
                    if (!setup_token) {
                        showError("Enter the one-time setup token from the server log.");
                        return;
                    }
                    if (!username || password.length < 8) {
                        showError("Enter a username and a password of at least 8 characters.");
                        return;
                    }
                    const r = await fetch("/setup", {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "Accept": "application/json" },
                        body: JSON.stringify({ setup_token, username, password }),
                    });
                    const j = await r.json();
                    if (!j.success) { showError(j.error || "Could not create account."); return; }
                    accountCreated = true;
                } else if (step === TOTAL) {
                    await post("/api/backfill/start", {});
                    window.location = "/";
                    return;
                } else {
                    // Save this step's credentials (no-op if all blank).
                    const creds = collectStepCreds(stepEl(step));
                    if (Object.keys(creds).length) await post("/api/settings/credentials", creds);
                }
                step = Math.min(TOTAL, step + 1);
                show();
            } catch (e) { showError(e.message); }
        }

        nextBtn.addEventListener("click", advance);
        skipBtn.addEventListener("click", () => { step = Math.min(TOTAL, step + 1); show(); });
        backBtn.addEventListener("click", () => { step = Math.max(1, step - 1); show(); });

        document.getElementById("su-test-sunsynk").addEventListener("click", async () => {
            flash("su-sunsynk-result", "testing…", true);
            try {
                const d = await post("/api/settings/test/sunsynk", collectStepCreds(stepEl(2)));
                flash("su-sunsynk-result", "OK — plants: " + d.plants.map((p) => p.name).join(", "), true);
            } catch (e) { flash("su-sunsynk-result", e.message, false); }
        });
        document.getElementById("su-discover").addEventListener("click", async () => {
            flash("su-discover-result", "discovering…", true);
            try {
                const m = await post("/api/settings/discover", collectStepCreds(stepEl(3)));
                const set = (k, v) => { const el = document.querySelector(`[data-cred="${k}"]`); if (el && v) el.value = v; };
                set("OCTOPUS_MPAN", m.import_mpan); set("OCTOPUS_METER_SERIAL", m.import_serial);
                set("OCTOPUS_EXPORT_MPAN", m.export_mpan); set("OCTOPUS_EXPORT_SERIAL", m.export_serial);
                set("OCTOPUS_MPRN", m.mprn); set("OCTOPUS_GAS_SERIAL", m.gas_serial);
                flash("su-discover-result", "Discovered region " + (m.region || "?") + " — review below.", true);
            } catch (e) { flash("su-discover-result", e.message, false); }
        });
        document.getElementById("su-test-webhook").addEventListener("click", async () => {
            try {
                const d = await post("/api/settings/test/webhook", collectStepCreds(stepEl(4)));
                flash("su-webhook-result", d.sent ? "Sent ✓" : ("Failed: " + d.detail), d.sent);
            } catch (e) { flash("su-webhook-result", e.message, false); }
        });

        show();
    }

    // ---- system flow tab ----
    function _flowKw(w) {
        if (w == null) return "–";
        return Math.abs(w) >= 1000 ? (Math.abs(w) / 1000).toFixed(2) + " kW"
                                   : Math.round(Math.abs(w)) + " W";
    }
    function _setFlow(lineId, valId, opt) {
        const ln = document.getElementById(lineId);
        const v = document.getElementById(valId);
        if (!ln) return;
        ln.classList.remove("active", "flow-fwd", "flow-rev");
        if (opt && opt.active) {
            ln.classList.add("active", opt.dir === "rev" ? "flow-rev" : "flow-fwd");
            ln.style.stroke = opt.color;
            ln.style.color = opt.color;   // drives the drop-shadow glow
            if (v) { v.textContent = opt.label; v.style.fill = opt.color; }
        } else {
            ln.style.stroke = "";
            if (v) v.textContent = "";
        }
    }
    let flowLoaded = false;
    async function pollFlow() {
        const TH = 50;   // ignore <50W noise
        try {
            const d = await api("/api/live");
            const msg = document.getElementById("flow-msg");
            if (!d.sunsynk) {
                // Only alarm before the first successful load; otherwise keep
                // the last-known flow visible (transient/rate-limited read).
                if (msg && !flowLoaded) { msg.style.display = ""; msg.textContent = d.sunsynk_error || "No live data"; }
            } else {
                flowLoaded = true;
                if (msg) msg.style.display = "none";
                const s = d.sunsynk, $ = (i) => document.getElementById(i);
                if (s.load_w != null) document.title = _flowKw(s.load_w) + " · PowerSOC";
                const C = (n) => cssVar(n);
                $("n-solar").textContent = _flowKw(s.pv_w);
                $("n-grid").textContent = _flowKw(s.grid_w);
                $("n-battery").textContent = _flowKw(s.battery_w);
                $("n-house").textContent = _flowKw(s.load_w);
                // A missing reading is "–", never "SoC 0%" (alarm-bait).
                $("n-hub").textContent = s.soc_pct != null ? "SoC " + Math.round(s.soc_pct) + "%" : "SoC –";
                // Represent multiple batteries / solar strings in the labels.
                $("n-battery-label").textContent =
                    s.battery_count > 1 ? "Battery ×" + s.battery_count : "Battery";
                $("n-solar-label").textContent = s.pv_string_count > 1
                    ? "Solar · " + s.pv_string_count + " strings" : "Solar";
                // solar -> hub (down)
                _setFlow("fl-solar", "fv-solar", s.pv_w > TH &&
                    { active: true, color: C("--c-solar"), dir: "fwd", label: "↓ " + _flowKw(s.pv_w) });
                // battery line battery->hub: fwd=discharge, rev=charge
                _setFlow("fl-battery", "fv-battery",
                    s.battery_w > TH ? { active: true, color: C("--c-battery"), dir: "rev", label: "charging" }
                    : s.battery_w < -TH ? { active: true, color: C("--c-battery"), dir: "fwd", label: "discharging" }
                    : { active: false });
                // grid line hub->grid: fwd=export, rev=import
                _setFlow("fl-grid", "fv-grid",
                    s.grid_w > TH ? { active: true, color: C("--c-import"), dir: "rev", label: "import" }
                    : s.grid_w < -TH ? { active: true, color: C("--c-export"), dir: "fwd", label: "export" }
                    : { active: false });
                // hub -> house (down)
                _setFlow("fl-house", "fv-house", s.load_w > TH &&
                    { active: true, color: C("--accent-3"), dir: "fwd", label: "↓ " + _flowKw(s.load_w) });
                const ts = $("flow-ts");
                if (ts) {
                    // Cached (rate-limited upstream) or served offline by the
                    // service worker: say so instead of "updated <now>".
                    const old = d.stale || d._offline;
                    ts.textContent = old ? "⚠️ showing last-known values"
                                         : "updated " + new Date().toLocaleTimeString();
                    ts.style.color = old ? "var(--c-peak)" : "";
                }
            }
            const tw = d.tariff, el = document.getElementById("flow-tariff");
            if (tw && el) {
                const r = tw.import_rate_p != null ? ` · ${tw.import_rate_p.toFixed(1)}p` : "";
                el.textContent = `${tw.window} window${r}`;
                el.className = "pill pill-" + tw.window;
            }
        } catch (e) { /* ignore transient */ }
        setTimeout(pollFlow, 60000);
    }
    function initFlow() { pollFlow(); }

    // ---- diagnostics (in-depth) ----
    async function pollInDepth() {
        const msg = document.getElementById("id-msg");
        try {
            const d = await api("/api/indepth");
            if (!d.configured) {
                if (msg) { msg.style.display = ""; msg.textContent = "Sunsynk not configured."; }
                return;   // no retry loop when unconfigured
            }
            if (msg) msg.style.display = "none";
            const b = d.battery || {};
            const u = (v, unit, dp) => v == null ? "–" : (+v).toFixed(dp == null ? 1 : dp) + unit;
            const cells = [
                ["State of charge", u(b.soc, "%", 0), "var(--c-battery)"],
                ["State of health (est.)", b.soh_pct != null ? b.soh_pct + "%"
                    : "set nameplate in Settings", "var(--c-battery)"],
                ["Cycle-equivalents", b.cycle_equivalents != null ? b.cycle_equivalents : "–", null],
                ["Power", u(b.power, " W", 0), null],
                ["Voltage", u(b.voltage, " V"), null],
                ["Current", u(b.current, " A"), null],
                ["Temperature", u(b.temp, " °C"), null],
                ["BMS SoC", u(b.bms_soc, "%", 0), null],
                ["BMS voltage", u(b.bms_voltage, " V"), null],
                ["BMS current", u(b.bms_current, " A"), null],
                ["BMS temp", u(b.bms_temp, " °C"), null],
                ["Capacity", u(b.capacity_ah, " Ah"), null],
                ["Corrected capacity", u(b.corrected_capacity_ah, " Ah"), null],
                ["Charge voltage", u(b.charge_volt, " V"), null],
                ["Charge current limit", u(b.charge_current_limit, " A"), null],
                ["Discharge current limit", u(b.discharge_current_limit, " A"), null],
                ["Charged today", u(b.today_charge_kwh, " kWh"), "var(--c-battery)"],
                ["Discharged today", u(b.today_discharge_kwh, " kWh"), null],
                ["Charged this month", u(b.month_charge_kwh, " kWh"), null],
                ["Discharged this month", u(b.month_discharge_kwh, " kWh"), null],
                ["Lifetime charged", u(b.total_charge_kwh, " kWh"), null],
                ["Lifetime discharged", u(b.total_discharge_kwh, " kWh"), null],
            ];
            document.getElementById("id-batt").innerHTML = cells.map(([l, v, c]) =>
                `<div class="card summary-card"${c ? ` style="--stripe:${c}"` : ""}>`
                + `<div class="label">${l}</div><div class="value" style="font-size:1.3rem">${v}</div></div>`).join("");

            const mc = document.getElementById("id-modules-card");
            if (b.modules && b.modules.length) {
                mc.style.display = "";
                document.querySelector("#id-modules tbody").innerHTML = b.modules.map((m) =>
                    `<tr><td>#${esc(m.n)}</td><td>${u(m.soc, "%", 0)}</td><td>${u(m.voltage, " V")}</td>`
                    + `<td>${u(m.current, " A")}</td><td>${u(m.power, " W", 0)}</td><td>${u(m.temp, " °C")}</td></tr>`).join("");
            } else { mc.style.display = "none"; }

            const pc = document.getElementById("id-pv-card");
            if (d.pv_strings && d.pv_strings.length) {
                pc.style.display = "";
                document.querySelector("#id-pv tbody").innerHTML = d.pv_strings.map((p) =>
                    `<tr><td>String ${esc(p.n)}</td><td>${u(p.voltage, " V")}</td>`
                    + `<td>${u(p.current, " A")}</td><td>${u(p.power, " W", 0)}</td>`
                    + `<td>${u(p.today_kwh, " kWh")}</td></tr>`).join("");
            } else { pc.style.display = "none"; }

            document.getElementById("id-ts").textContent = "updated " + new Date().toLocaleTimeString();
        } catch (e) {
            if (msg) { msg.style.display = ""; msg.textContent = "Read unavailable (will retry)."; }
        }
        setTimeout(pollInDepth, 3600000);   // battery detail refreshes hourly
    }
    async function loadBatteryHealth() {
        try {
            const h = await api("/api/insights/battery-health");
            const card = document.getElementById("id-health-card");
            if (!card || !h.available) return;
            document.getElementById("id-health-summary").textContent =
                `Measured from SoC falls of 30 points or more: ~${h.trend_kwh} kWh usable`
                + (h.soh_pct != null ? ` — ${h.soh_pct}% of the ${h.nameplate_kwh} kWh nameplate` : "")
                + ` (median of the latest ${Math.min(14, h.estimates.length)} estimates).`;
            makeChart("chart-capacity", "line", h.estimates.map((e) => e.date), [
                ds("Measured usable kWh", h.estimates.map((e) => e.kwh), "battery",
                   { pointRadius: 2, tension: 0.2 }),
            ], false, "kWh");
            card.style.display = "";
        } catch (e) { /* optional */ }
    }

    async function loadGrid() {
        try {
            const g = await api("/api/power-quality?days=7");
            const card = document.getElementById("id-grid-card");
            if (!card || !g.summary.samples) return;
            const s = g.summary;
            document.getElementById("id-grid-summary").textContent =
                `Last 7 days: ${s.min_v ?? "–"}–${s.max_v ?? "–"} V (avg ${s.avg_v ?? "–"} V), `
                + `${s.min_hz ?? "–"}–${s.max_hz ?? "–"} Hz · ${s.outages} outage(s), `
                + `${s.outage_minutes} min without grid. UK limits: 216–253 V, 49.5–50.5 Hz.`;
            makeChart("chart-grid", "line", g.series.map((r) => r.slot.slice(5, 16).replace("T", " ")), [
                ds("Max V", g.series.map((r) => r.max_v), "peak", { pointRadius: 0 }),
                ds("Min V", g.series.map((r) => r.min_v), "import", { pointRadius: 0 }),
            ], false, "V");
            charts["chart-grid"].options.scales.y.beginAtZero = false;
            charts["chart-grid"].update();
            document.querySelector("#id-grid-events tbody").innerHTML = g.events.length
                ? g.events.map((e) => `<tr><td>${esc(e.kind)}${e.ongoing ? " (ongoing)" : ""}</td>`
                    + `<td>${esc(new Date(e.start).toLocaleString())}</td><td>${e.minutes} min</td>`
                    + `<td>${e.min_v ?? "–"}–${e.max_v ?? "–"} V</td>`
                    + `<td>${e.min_hz ?? "–"}–${e.max_hz ?? "–"} Hz</td></tr>`).join("")
                : `<tr><td colspan="5" class="muted">No outages or out-of-limit readings in the last 30 days.</td></tr>`;
            card.style.display = "";
        } catch (e) { /* optional */ }
    }
    function initInDepth() { pollInDepth(); loadBatteryHealth(); loadGrid(); }

    // ---- data health page (quick win 4) ----
    async function initData() {
        const chips = document.getElementById("health-chips");
        try {
            const h = await api("/api/health");
            const chip = (ok, text) => `<span class="pill ${ok === false ? "pill-peak"
                : ok === null || ok === undefined ? "" : "pill-cheap"}">${esc(text)}</span>`;
            chips.innerHTML = [
                chip(h.credentials_ok, h.credentials_ok ? "credentials readable" : "credentials unreadable"),
                chip(h.scheduler_ok, h.scheduler_ok === false ? "scheduler stalled" : "scheduler running"),
                chip(h.brief_ok, h.brief_ok === null ? "brief: not this instance's job"
                    : h.brief_ok ? `brief delivered (${(h.brief || {}).last_delivered || "–"})`
                    : `brief NOT delivered (last ${(h.brief || {}).last_delivered || "never"})`),
                chip(h.backup_ok, h.backup_ok ? "backup fresh" : "backup missing/failed"),
                chip(h.backup_isolated ? true : false, h.backup_isolated
                    ? "backups on another disk" : "backups share the data disk"),
                chip(null, `${(h.instance || {}).name || ""} · ${(h.instance || {}).role || ""} · v${h.version}`),
            ].join(" ");
        } catch (e) { chips.textContent = "Health unavailable"; }

        async function coverage() {
            const days = document.getElementById("coverage-days").value;
            const grid = document.getElementById("coverage-grid");
            try {
                const c = await api("/api/coverage?days=" + days);
                grid.innerHTML = c.days.map((d) => {
                    const detail = ["sunsynk", "elec", "export", "gas"]
                        .map((k) => `${k} ${d[k]}/${d.expected}`).join(" · ");
                    return `<span class="cov cov-${d.status}" title="${esc(d.date)}: ${esc(detail)}"></span>`;
                }).join("");
                document.getElementById("coverage-legend").dataset.sources = c.sources.join(",");
            } catch (e) { grid.textContent = "Coverage unavailable"; }
        }
        document.getElementById("coverage-days").addEventListener("change", coverage);
        coverage();

        try {
            const ct = await api("/api/insights/ct-check");
            document.getElementById("ct-summary").textContent = ct.available
                ? ct.summary + (ct.flagged.length ? ` — ${ct.flagged.length} day(s) more than 5% apart (below).` : ".")
                : "Not enough complete days with both the meter and the CT yet.";
            if (ct.available && ct.flagged.length) {
                document.getElementById("ct-table").style.display = "";
                document.querySelector("#ct-table tbody").innerHTML = ct.flagged.map((d) =>
                    `<tr><td>${esc(d.date)}</td><td>${d.meter_kwh}</td><td>${d.ct_kwh}</td>`
                    + `<td>${(d.ratio * 100).toFixed(1)}%</td></tr>`).join("");
            }
        } catch (e) { /* optional */ }

        try {
            const rows = await api("/api/sync-log?limit=100");
            document.querySelector("#synclog-table tbody").innerHTML = rows.map((r) =>
                `<tr><td>${esc((r.run_at || "").replace("T", " ").slice(0, 16))}</td><td>${esc(r.source)}</td>`
                + `<td>${esc(r.date_fetched || "")}</td>`
                + `<td><span class="pill ${r.status === "error" ? "pill-peak" : r.status === "partial" ? "pill-day" : "pill-cheap"}">${esc(r.status)}</span></td>`
                + `<td>${r.intervals_written ?? ""}</td><td>${esc(r.message || "")}</td></tr>`).join("");
        } catch (e) { /* optional */ }

        const auditCard = document.getElementById("audit-card");
        if (auditCard) {
            async function audit() {
                const a = document.getElementById("audit-action").value;
                try {
                    const rows = await api("/api/audit?limit=200" + (a ? "&action=" + encodeURIComponent(a) : ""));
                    document.querySelector("#audit-table tbody").innerHTML = rows.map((r) =>
                        `<tr><td>${esc(r.created_at)}</td><td>${esc(r.username || "–")}</td>`
                        + `<td>${esc(r.action)}</td><td>${esc([r.target_type, r.target_id].filter(Boolean).join(" "))}</td>`
                        + `<td><code>${esc(r.details || "")}</code></td><td>${esc(r.ip_address || "")}</td></tr>`).join("");
                } catch (e) { /* ignore */ }
            }
            document.getElementById("audit-action").addEventListener("change", audit);
            audit();
        }
    }

    return { initDashboard: initDashboard, initHistory: initHistory, initData: initData,
             initSettings: initSettings, initSetup: initSetup, initFlow: initFlow,
             initInDepth: initInDepth, loadDay: loadDay, api: api, cssVar: cssVar };
})();
