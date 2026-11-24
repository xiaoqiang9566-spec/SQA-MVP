import { buildComparisonDailyRows, buildPeriodSeries, getFilterOptions, listPeriods } from "./data.mjs";
import { openSnapshot } from "./snapshot.mjs";

const $ = (selector) => document.querySelector(selector);
const dimensions = { firmware_version: "固件版本", user_region: "地区", model_name: "手表机型", mobile_platform: "手机平台", app_version: "Suunto App 版本" };
const periodNames = { day: "日", week: "周", month: "月", quarter: "季度" };
const colors = ["#176b50", "#c06c27", "#326b9b", "#a24c67", "#785ba4", "#26888c", "#99802b"];
let cohorts = [];
let bounds;
let firmwareStartDays = {};
let selectedDimension = "firmware_version";
let selectedPeriod = "week";
let filters = {};
let hiddenGroups = new Set();
let cachedView = null;
let pendingFrame;
const widgets = new Map();
let signatureCohorts = [];
let signatureModule;
let signatureState = "loading";
let signatureError = "";
let signatureQueryTime = "";
let signatureLoading = false;
let signatureLoadAttempt = 0;
let signatureRevision = 0;
let signatureCache = null;
let signaturePeriod = "all";
let signatureLimit = 50;
let mainLoadError = "";
let snapshot;
let coveredPlatforms = ["android"];
let queriedDays;
const preferenceKey = "sqa-crash-view-v1:" + new URLSearchParams(location.search).get("viewer");
let preferenceReady = false;

function savePreferences() {
  if (!preferenceReady) return;
  const state = {schema:1, filters, selectedDimension, selectedPeriod, hiddenGroups:[...hiddenGroups],
    startDate:$("#start-date").value, endDate:$("#end-date").value, preset:$("#date-preset").value,
    boundStart:bounds.first, boundEnd:bounds.last, signaturePeriod,
    signatureSearch:$("#signature-search").value, countDetails:$("#count-details").open};
  try { localStorage.setItem(preferenceKey,JSON.stringify(state)); }
  catch { $("#preference-notice").textContent = "浏览器未允许保存筛选条件，重载后可能恢复默认值。"; }
}
window.saveSqaCrashPreferences = savePreferences;
for (const event of ["change","input","click","keyup"]) document.addEventListener(event,savePreferences);
window.addEventListener("pagehide",savePreferences);

function restorePreferences() {
  try {
    const state = JSON.parse(localStorage.getItem(preferenceKey));
    if (!state || state.schema !== 1) return;
    const list = values => Array.isArray(values) && values.length <= 500 && values.every(v=>typeof v === "string" && v.length<=500);
    for (const key of Object.keys(dimensions)) if (list(state.filters?.[key])) filters[key] = state.filters[key];
    if (Object.hasOwn(dimensions,state.selectedDimension)) selectedDimension=state.selectedDimension;
    if (Object.hasOwn(periodNames,state.selectedPeriod)) selectedPeriod=state.selectedPeriod;
    if (list(state.hiddenGroups)) hiddenGroups=new Set(state.hiddenGroups);
    if (/^\d{4}-\d{2}-\d{2}$/.test(state.startDate) && /^\d{4}-\d{2}-\d{2}$/.test(state.endDate)) {
      $("#start-date").value=state.startDate;
      $("#end-date").value=state.endDate;
      // Preserve the exact dates. A changed snapshot turns a relative preset into a fixed custom range.
      $("#date-preset").value=state.boundStart===bounds.first && state.boundEnd===bounds.last && ["30","90","all","custom"].includes(state.preset) ? state.preset : "custom";
    }
    if (typeof state.signaturePeriod === "string") signaturePeriod=state.signaturePeriod;
    if (typeof state.signatureSearch === "string") $("#signature-search").value=state.signatureSearch.slice(0,500);
    $("#count-details").open=state.countDetails===true;
    updateButtons("dimension",selectedDimension);
    updateButtons("period",selectedPeriod);
    $("#preference-notice").textContent="已恢复本账号在当前浏览器选择的日期与筛选条件。";
  } catch { /* A missing or invalid preference uses the Race 3S defaults. */ }
}

$("#signature-search").addEventListener("input", () => { signatureLimit = 50; renderSignatures(); });
$("#signature-period").addEventListener("change", () => { signaturePeriod = $("#signature-period").value; signatureLimit = 50; renderSignatures(); });
$("#signature-more").addEventListener("click", () => { signatureLimit += 50; renderSignatures(); });
$("#signature-retry").addEventListener("click", () => { void loadSignatures(); });

initialize().catch((error) => {
  $("#load-status").textContent = "数据载入失败：" + error.message;
  $("#load-status").classList.add("is-error");
  mainLoadError = error.message;
  signatureState = "failed";
  renderSignatures();
});

async function initialize() {
  snapshot = await openSnapshot();
  const { manifest } = snapshot;
  coveredPlatforms = snapshot.coveredPlatforms;
  queriedDays = snapshot.pointer ? manifest.queried_days : undefined;
  cohorts = await snapshot.loadTrendRows();
  bounds = { first: manifest.start_date, last: manifest.end_date };
  firmwareStartDays = Object.fromEntries((manifest.firmware_scope || []).map((entry) => [entry.firmware_version, entry.scope_start_utc]));
  $("#snapshot-date").textContent = bounds.last;
  $("#snapshot-badge").setAttribute("aria-label", "数据截止 " + bounds.last + " UTC");
  for (const input of [$("#start-date"), $("#end-date")]) { input.min = bounds.first; input.max = bounds.last; }
  $("#start-date").value = bounds.first;
  $("#end-date").value = bounds.last;
  resetFilters();
  restorePreferences();
  createFilterWidgets();
  setupControls();
  updateData();
  preferenceReady = true;
  savePreferences();
  new ResizeObserver(() => {
    cancelAnimationFrame(pendingFrame);
    pendingFrame = requestAnimationFrame(() => { if (cachedView) renderView(); });
  }).observe($(".chart-frame"));
  $("#reset-filters").disabled = false;
  const publication = snapshot.pointer ? "发布已验证 · " + snapshot.pointer.published_at_utc + " · 快照 " + snapshot.pointer.snapshot_id : "旧 v1 历史快照 · 未使用 latest 发布指针";
  $("#load-status").textContent = "历史数据：" + bounds.first + " 至 " + bounds.last + " UTC · 数据截止 " + bounds.last + " UTC · 查询时间 " + queryTime(manifest) + " · " + publication + " · 刷新页面读取最新发布。";
  void loadSignatures();
}

async function loadSignatures() {
  if (signatureLoading || mainLoadError || !bounds) return;
  signatureLoading = true;
  signatureState = "loading";
  signatureError = "";
  signatureCache = null;
  renderSignatures();
  try {
    signatureLoadAttempt += 1;
    const module = await import(signatureLoadAttempt === 1 ? "./signatures.mjs" : "./signatures.mjs?retry=" + signatureLoadAttempt);
    const parsed = await snapshot.loadSignatureRows(module.parseSignatureRows);
    // 所有分片及解析核验通过后一次替换，禁止展示半份快照。
    signatureModule = module;
    signatureCohorts = parsed;
    signatureQueryTime = queryTime(snapshot.signatureManifest);
    signatureRevision += 1;
    signatureLimit = 50;
    signatureState = "ready";
  } catch (error) {
    signatureState = "failed";
    signatureError = error.message;
    signatureCohorts = [];
  } finally {
    signatureLoading = false;
    renderSignatures();
  }
}

function resetFilters() {
  filters = Object.fromEntries(Object.keys(dimensions).map((field) => [field, []]));
  filters.firmware_version = ["2.57.62", "2.57.64"];
  filters.mobile_platform = [...coveredPlatforms];
  hiddenGroups.clear();
  signaturePeriod = "all";
  signatureLimit = 50;
  signatureCache = null;
  $("#signature-search").value = "";
}

function createFilterWidgets() {
  for (const [field, label] of Object.entries(dimensions)) {
    const wrapper = document.createElement("div");
    wrapper.className = "filter-field";
    const heading = document.createElement("span");
    heading.className = "field-label"; heading.textContent = label;
    const details = document.createElement("details");
    details.className = "filter-dropdown"; details.dataset.filter = field;
    const summary = document.createElement("summary"); summary.setAttribute("aria-label", label + "筛选");
    const menu = document.createElement("div"); menu.className = "filter-menu";
    const search = document.createElement("input");
    search.type = "search"; search.placeholder = "搜索" + label; search.setAttribute("aria-label", "搜索" + label);
    const clear = document.createElement("button");
    clear.type = "button"; clear.className = "text-button"; clear.textContent = "不限此维度";
    const options = document.createElement("div"); options.className = "filter-options";
    menu.append(search, clear, options); details.append(summary, menu); wrapper.append(heading, details);
    $("#filter-fields").append(wrapper);
    widgets.set(field, { summary, search, options, details, available: [] });
    search.addEventListener("input", () => renderFilterOptions(field));
    clear.addEventListener("click", () => { filters[field] = []; hiddenGroups.clear(); updateData(); });
    options.addEventListener("change", (event) => {
      const checkbox = event.target;
      if (!(checkbox instanceof HTMLInputElement)) return;
      const selected = new Set(filters[field]);
      if (checkbox.checked) selected.add(checkbox.value); else selected.delete(checkbox.value);
      filters[field] = [...selected];
      hiddenGroups.clear();
      updateData();
    });
    details.addEventListener("toggle", () => {
      if (details.open) for (const [other, widget] of widgets) if (other !== field) widget.details.open = false;
    });
  }
  document.addEventListener("click", (event) => {
    for (const widget of widgets.values()) if (!widget.details.contains(event.target)) widget.details.open = false;
  });
}

function renderFilterOptions(field) {
  const widget = widgets.get(field);
  const focusedValue = widget.options.contains(document.activeElement) ? document.activeElement.value : null;
  const selected = new Set(filters[field]);
  const available = new Set(widget.available);
  const values = orderGroups([...new Set([...widget.available, ...selected])], field)
    .filter((value) => value.toLocaleLowerCase().includes(widget.search.value.toLocaleLowerCase()));
  const fragment = document.createDocumentFragment();
  for (const value of values) {
    const label = document.createElement("label"); label.className = "filter-option";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.value = value; checkbox.checked = selected.has(value);
    checkbox.disabled = !available.has(value) && !selected.has(value);
    const text = document.createElement("span"); text.textContent = value + (!available.has(value) ? "（当前组合无样本）" : "");
    label.append(checkbox, text); fragment.append(label);
  }
  if (!values.length) { const note = document.createElement("p"); note.textContent = "没有匹配的可选值"; fragment.append(note); }
  widget.options.replaceChildren(fragment);
  if (focusedValue != null) [...widget.options.querySelectorAll("input")].find((input) => input.value === focusedValue)?.focus({ preventScroll: true });
  widget.summary.textContent = selected.size ? [...selected].join("、") : "全部";
  widget.summary.title = widget.summary.textContent;
}

function setupControls() {
  document.querySelectorAll("[data-dimension]").forEach((button) => button.addEventListener("click", () => {
    selectedDimension = button.dataset.dimension; hiddenGroups.clear(); updateButtons("dimension", selectedDimension); updateData();
  }));
  document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
    signaturePeriod = "all";
    selectedPeriod = button.dataset.period; updateButtons("period", selectedPeriod); updateData(false);
  }));
  $("#reset-filters").addEventListener("click", () => { resetFilters(); updateData(); });
  $("#date-preset").addEventListener("change", () => {
    const preset = $("#date-preset").value;
    if (preset === "custom") return;
    $("#end-date").value = bounds.last;
    const start = new Date(bounds.last + "T00:00:00Z");
    if (preset !== "all") start.setUTCDate(start.getUTCDate() - Number(preset) + 1);
    $("#start-date").value = preset === "all" ? bounds.first : [start.toISOString().slice(0, 10), bounds.first].sort().at(-1);
    updateData();
  });
  for (const input of [$("#start-date"), $("#end-date")]) input.addEventListener("change", () => {
    $("#date-preset").value = "custom"; updateData();
  });
  $("#count-details").addEventListener("toggle", () => { if ($("#count-details").open && cachedView) renderCountTable(); });
}

function updateButtons(attribute, selected) {
  document.querySelectorAll("[data-" + attribute + "]").forEach((button) => {
    const active = button.dataset[attribute] === selected;
    button.classList.toggle("is-active", active); button.setAttribute("aria-pressed", String(active));
  });
}

function updateData(refreshOptions = true) {
  const startDate = $("#start-date").value; const endDate = $("#end-date").value;
  const error = $("#date-error");
  const invalid = !startDate || !endDate || startDate > endDate || startDate < bounds.first || endDate > bounds.last;
  error.hidden = !invalid;
  if (invalid) {
    error.textContent = "请选择 " + bounds.first + " 至 " + bounds.last + " 范围内有效的起止日期，开始日期不能晚于结束日期。";
    cachedView = null; $("#trend-chart").replaceChildren(); $("#chart-legend").replaceChildren(); $("#comparison-head").replaceChildren(); $("#trend-table").replaceChildren(); $("#count-table").replaceChildren();
    $("#chart-summary").textContent = "日期范围无效，请调整后查看。"; $("#series-count").textContent = ""; renderSignatures(); return;
  }
  if (refreshOptions) {
    const options = getFilterOptions(cohorts, { filters, startDate, endDate });
    for (const field of Object.keys(dimensions)) { widgets.get(field).available = options[field]; renderFilterOptions(field); }
  }
  const rows = buildComparisonDailyRows(cohorts, { filters, groupField: selectedDimension, startDate, endDate, firmwareStartDays, coveredPlatforms, queriedDays });
  const series = buildPeriodSeries(rows, { period: selectedPeriod, startDate, endDate, fallbackGroups: filters[selectedDimension] });
  const groups = orderGroups([...new Set(series.map((point) => point.group))], selectedDimension);
  const periods = listPeriods({ period: selectedPeriod, startDate, endDate });
  const beforeScope = filters.firmware_version.length > 0 && filters.firmware_version.every((version) => firmwareStartDays[version] > endDate);
  const emptyReason = beforeScope ? "固件在所选日期尚未纳入范围" : "当前筛选没有同步观测";
  const periodRanges = new Map();
  for (const point of series) {
    if (periodRanges.has(point.eventDay)) continue;
    periodRanges.set(point.eventDay, {
      startDate: point.periodStartDay > startDate ? point.periodStartDay : startDate,
      endDate: point.periodEndDay < endDate ? point.periodEndDay : endDate,
    });
  }
  cachedView = { series, groups, periods, periodRanges, startDate, endDate, emptyReason };
  signatureLimit = 50;
  const description = Object.entries(dimensions).map(([field, label]) => label + "：" + (filters[field].length ? filters[field].join("、") : "全部")).join(" · ");
  $("#filter-summary").textContent = description;
  $("#chart-title").textContent = dimensions[selectedDimension] + "对比";
  $("#chart-summary").textContent = startDate + " 至 " + endDate + " UTC · 按" + periodNames[selectedPeriod] + " · " + periods.length + " 个周期 · " + platformLabel() + " 设备日加权占比";
  renderView();
}

function renderView() {
  const { series, groups, periods } = cachedView;
  const visible = groups.filter((group) => !hiddenGroups.has(group));
  const points = series.filter((point) => !hiddenGroups.has(point.group));
  $("#chart-summary").textContent = cachedView.startDate + " 至 " + cachedView.endDate + " UTC · 按" + periodNames[selectedPeriod] + " · " + periods.length + " 个周期 · " + platformLabel() + " 设备日加权占比";
  document.querySelectorAll("[data-covered-label]").forEach((element) => { element.textContent = platformLabel() + " 同步设备日"; });
  document.querySelectorAll("[data-covered-share-label]").forEach((element) => { element.textContent = "占 " + platformLabel() + " 同步设备日"; });
  $("#metric-note").textContent = "占比 = 同日匹配崩溃设备日 ÷ " + platformLabel() + " 同步设备日。周、月、季度按设备日加权；跨平台合看按设备日并集去重。空心点表示部分周期；未覆盖平台只保留同步样本，崩溃率留空。不同分组可能共享设备，不能相加为总量。";
  $("#series-count").textContent = visible.length + " / " + groups.length + " 个对比组";
  renderChart(points, visible, periods, groups);
  renderLegend(groups);
  renderComparisonTable(points, visible, periods, groups);
  if ($("#count-details").open) renderCountTable(); else $("#count-table").replaceChildren();
  renderSignatures();
}

function colorFor(group, groups) {
  const index = groups.indexOf(group);
  return index < colors.length ? colors[index] : "hsl(" + ((index * 137.508 + 155) % 360) + " 48% 40%)";
}

function renderChart(series, groups, periods, allGroups) {
  const svg = $("#trend-chart");
  const width = Math.max(svg.parentElement.clientWidth, 280); const height = width < 560 ? 300 : 380;
  const margin = { top: 28, right: 22, bottom: 45, left: width < 560 ? 60 : 72 };
  const plotWidth = width - margin.left - margin.right; const plotHeight = height - margin.top - margin.bottom;
  const rates = series.map((point) => point.ratePct).filter((rate) => rate != null);
  const maximum = Math.max(0, ...rates); const yMax = maximum ? maximum * 1.12 : 1;
  const xFor = (index) => margin.left + (periods.length < 2 ? plotWidth / 2 : index / (periods.length - 1) * plotWidth);
  const yFor = (rate) => margin.top + plotHeight - rate / yMax * plotHeight;
  svg.replaceChildren(); svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.style.height = height + "px";
  svg.setAttribute("aria-label", dimensions[selectedDimension] + "按" + periodNames[selectedPeriod] + "崩溃设备日占比对比");
  addText(svg, margin.left, 16, "崩溃设备日占比（%）", "start");
  for (let tick = 0; tick <= 4; tick++) {
    const value = yMax * tick / 4; const y = yFor(value);
    svg.append(makeSvgElement("line", { x1: margin.left, y1: y, x2: width - margin.right, y2: y, class: "grid-line" }));
    addText(svg, margin.left - 8, y + 4, formatRate(value), "end");
  }
  const step = Math.max(1, Math.ceil(periods.length / Math.max(2, Math.floor(plotWidth / 94))));
  periods.forEach((period, index) => {
    if (index % step && index !== periods.length - 1) return;
    if (index !== periods.length - 1 && periods.length - 1 - index < step * 0.7) return;
    addText(svg, xFor(index), height - 18, period, index === 0 ? "start" : index === periods.length - 1 ? "end" : "middle");
  });
  const periodIndices = new Map(periods.map((period, index) => [period, index]));
  for (const group of groups) {
    const groupPoints = series.filter((point) => point.group === group);
    const color = colorFor(group, allGroups); let path = ""; let connected = false;
    for (const point of groupPoints) {
      if (point.ratePct == null) { connected = false; continue; }
      const x = xFor(periodIndices.get(point.eventDay)); const y = yFor(point.ratePct);
      path += (connected ? " L" : " M") + x + "," + y; connected = true;
    }
    if (path) svg.append(makeSvgElement("path", { d: path, class: "series-line", stroke: color, "data-group": group }));
    for (const point of groupPoints) {
      if (point.ratePct == null) continue;
      const circle = makeSvgElement("circle", { cx: xFor(periodIndices.get(point.eventDay)), cy: yFor(point.ratePct), r: 3.8, class: "point", fill: point.status === "PARTIAL_PERIOD" ? "#fff" : color, stroke: color, tabindex: "0", role: "button", "aria-controls": "signature-section", "data-group": group, "data-period": point.eventDay, "aria-label": pointLabel(point), "aria-description": "激活查看本周期崩溃类型" });
      circle.addEventListener("click", () => locateSignaturePeriod(point.eventDay));
      circle.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") { event.preventDefault(); locateSignaturePeriod(point.eventDay); }
      });
      const title = makeSvgElement("title"); title.textContent = pointLabel(point); circle.append(title); svg.append(circle);
    }
  }
  if (!rates.length) addText(svg, width / 2, height / 2, groups.length ? "当前筛选无可绘制占比，请查看计数及覆盖状态" : allGroups.length ? "全部曲线已隐藏" : cachedView.emptyReason, "middle", "no-observations");
}

function renderLegend(groups) {
  const focusedGroup = $("#chart-legend").contains(document.activeElement) ? document.activeElement.dataset.group : null;
  const fragment = document.createDocumentFragment();
  for (const group of groups) {
    const button = document.createElement("button"); button.type = "button"; button.className = "legend-item";
    const visible = !hiddenGroups.has(group); button.classList.toggle("is-muted", !visible); button.setAttribute("aria-pressed", String(visible)); button.dataset.group = group;
    const swatch = document.createElement("span"); swatch.className = "legend-swatch"; swatch.style.background = colorFor(group, groups);
    const text = document.createElement("span");
    const points = cachedView.series.filter((point) => point.group === group);
    const uncovered = points.some((point) => point.status === "CRASH_RATE_NOT_COVERED_BY_ANDROID_RULE");
    text.textContent = group + (uncovered ? "（崩溃率未覆盖）" : "");
    button.append(swatch, text);
    button.addEventListener("click", () => { if (hiddenGroups.has(group)) hiddenGroups.delete(group); else hiddenGroups.add(group); renderView(); });
    fragment.append(button);
  }
  $("#chart-legend").replaceChildren(fragment);
  if (focusedGroup != null) [...$("#chart-legend").querySelectorAll("button")].find((button) => button.dataset.group === focusedGroup)?.focus({ preventScroll: true });
}

function renderComparisonTable(series, groups, periods, allGroups) {
  const header = document.createElement("tr"); header.append(cell("th", "周期（UTC）", "col"));
  if (!groups.length) header.append(cell("th", "数据状态", "col"));
  for (const group of groups) {
    const th = cell("th", group, "col"); th.className = "numeric"; th.style.borderTop = "3px solid " + colorFor(group, allGroups); header.append(th);
  }
  $("#comparison-head").replaceChildren(header);
  const points = new Map(series.map((point) => [point.eventDay + "\u0000" + point.group, point]));
  const fragment = document.createDocumentFragment();
  for (const period of periods) {
    const tr = document.createElement("tr"); tr.append(cell("th", period, "row"));
    if (!groups.length) tr.append(cell("td", allGroups.length ? "全部曲线已隐藏" : cachedView.emptyReason));
    for (const group of groups) {
      const point = points.get(period + "\u0000" + group);
      const td = cell("td", point.ratePct == null ? "—" : formatRate(point.ratePct) + "%" + (point.status === "PARTIAL_PERIOD" ? " ◇" : ""));
      td.className = "numeric"; td.title = pointLabel(point); td.dataset.group = group; td.dataset.period = period;
      if (point.ratePct == null) { const reason = document.createElement("small"); reason.textContent = statusLabel(point.status); td.append(reason); }
      tr.append(td);
    }
    fragment.append(tr);
  }
  $("#trend-table").replaceChildren(fragment);
}

function renderCountTable() {
  const fragment = document.createDocumentFragment();
  for (const point of cachedView.series) {
    if (hiddenGroups.has(point.group)) continue;
    const tr = document.createElement("tr");
    const values = [point.eventDay, point.group, point.ratePct == null ? "—" : formatRate(point.ratePct) + "%", formatCount(point.numerator), formatCount(point.denominator), formatCount(point.allSyncedDevices), point.observedDays + " / " + point.selectedDays + " / " + point.periodDays, statusLabel(point.status)];
    values.forEach((value, index) => { const td = cell("td", value); if (index > 1 && index < 7) td.className = "numeric"; tr.append(td); });
    fragment.append(tr);
  }
  $("#count-table").replaceChildren(fragment);
}

function locateSignaturePeriod(period) {
  if (!cachedView?.periodRanges.has(period)) return;
  signaturePeriod = period;
  signatureLimit = 50;
  renderSignatures();
  $("#signature-section").focus({ preventScroll: true });
  $("#signature-section").scrollIntoView({ block: "start", behavior: "auto" });
}

function signatureEmpty(message) {
  const tr = document.createElement("tr");
  const td = cell("td", message); td.colSpan = 5; td.className = "signature-empty";
  tr.append(td); $("#signature-table").replaceChildren(tr);
  $("#signature-count").textContent = "";
  $("#signature-more").hidden = true;
}

function renderSignatures() {
  const status = $("#signature-status");
  status.dataset.state = signatureState;
  status.classList.toggle("is-error", signatureState === "failed");
  status.textContent = mainLoadError ? "类型数据加载失败：趋势数据不可用，请重新载入页面。"
    : signatureState === "loading" ? "类型数据加载中…"
      : signatureState === "failed" ? "类型数据加载失败：" + signatureError
        : "类型数据可用 · " + bounds.first + " 至 " + bounds.last + " UTC · 查询时间 " + signatureQueryTime;
  $("#signature-section").setAttribute("aria-busy", String(signatureState === "loading"));
  $("#signature-retry").hidden = signatureState !== "failed" || Boolean(mainLoadError);
  const ready = signatureState === "ready" && Boolean(cachedView) && !mainLoadError;
  $("#signature-search").disabled = !ready;
  $("#signature-period").disabled = !ready;
  const options = [new Option("全部日期", "all")];
  if (cachedView) for (const [label, range] of cachedView.periodRanges) {
    options.push(new Option(label + "（" + range.startDate + " 至 " + range.endDate + "）", label));
  }
  if (!cachedView?.periodRanges.has(signaturePeriod)) signaturePeriod = "all";
  $("#signature-period").replaceChildren(...options);
  $("#signature-period").value = signaturePeriod;
  $("#signature-summary").textContent = "";
  $("#signature-summary").classList.remove("is-error");
  if (mainLoadError) { signatureEmpty("趋势数据载入失败，无法核对类型分母；请重新载入页面。"); return; }
  if (!cachedView) { signatureEmpty(bounds ? "日期范围无效，请调整后查看崩溃类型。" : "等待趋势数据载入后显示崩溃类型。"); return; }
  if (signatureState !== "ready") {
    signatureEmpty(signatureState === "failed" ? "类型数据载入失败，可重试；趋势图与周期对比仍可使用。" : "正在完整加载类型快照，请稍候…"); return;
  }
  const visibleGroups = cachedView.groups.filter((group) => !hiddenGroups.has(group));
  if (cachedView.groups.length && !visibleGroups.length) { signatureEmpty("全部图例已隐藏，请显示至少一个对比组查看类型。"); return; }
  const unsupportedPlatform = filters.mobile_platform.length > 0 && !filters.mobile_platform.some((platform) => coveredPlatforms.includes(platform))
    || selectedDimension === "mobile_platform" && visibleGroups.length > 0 && !visibleGroups.some((platform) => coveredPlatforms.includes(platform));
  if (unsupportedPlatform) { signatureEmpty("当前平台不在 " + platformLabel(coveredPlatforms) + " 崩溃匹配规则覆盖范围，类型与占比不可用。"); return; }
  const range = signaturePeriod === "all" ? { startDate: cachedView.startDate, endDate: cachedView.endDate } : cachedView.periodRanges.get(signaturePeriod);
  const cacheKey = JSON.stringify([signatureRevision, filters, selectedDimension, visibleGroups, range]);
  let summary;
  try {
    if (signatureCache?.key === cacheKey) summary = signatureCache.summary;
    else {
      summary = signatureModule.buildSignatureSummary(signatureCohorts, cohorts, { filters, groupField: selectedDimension, visibleGroups, coveredPlatforms, ...range });
      signatureCache = { key: cacheKey, summary };
      signatureLimit = 50;
    }
  } catch (error) {
    signatureState = "failed"; signatureError = "类型汇总不可用：" + error.message;
    signatureCache = null; renderSignatures(); return;
  }
  $("#signature-summary").textContent = (signaturePeriod === "all" ? "全部日期" : signaturePeriod) + " · " + range.startDate + " 至 " + range.endDate + " UTC · 匹配崩溃设备日 " + formatCount(summary.matchedDeviceDays) + " · " + platformLabel() + " 同步设备日 " + formatCount(summary.syncedDeviceDays) + (summary.consistent ? "" : " · 对账失败：类型覆盖与匹配崩溃设备日不一致，百分比不可用");
  $("#signature-summary").classList.toggle("is-error", !summary.consistent);
  if (!summary.syncedDeviceDays) { signatureEmpty("当前筛选或周期无 " + platformLabel() + " 同步样本，类型占比不可用。"); return; }
  if (!summary.matchedDeviceDays && summary.consistent) { signatureEmpty("当前筛选或周期有同步样本，但无匹配崩溃设备日（0）；暂无崩溃类型。"); return; }
  const query = $("#signature-search").value.trim().toLocaleLowerCase();
  const matching = summary.rows.filter((row) => row.signature.toLocaleLowerCase().includes(query));
  if (!matching.length) signatureEmpty(summary.rows.length ? "没有匹配搜索条件的类型；分母保持不变。" : summary.consistent ? "当前范围没有可展示的崩溃类型。" : "对账失败，类型数据缺失；请重试加载类型快照。");
  else {
    const fragment = document.createDocumentFragment();
    for (const row of matching.slice(0, signatureLimit)) {
      const tr = document.createElement("tr"); tr.dataset.signature = row.signature;
      const signature = cell("td", row.signature || "（未记录签名）"); signature.className = "signature-value";
      const kind = cell("td", { SIGNATURE_CANDIDATE: "签名", LOG_FRAGMENT: "日志片段", MISSING_SIGNATURE: "缺少签名" }[row.recordKind] || "未分类"); kind.className = "signature-kind";
      tr.append(signature, kind);
      for (const value of [formatCount(row.deviceDays), summary.consistent && row.sharePct != null ? formatRate(row.sharePct) + "%" : "—", summary.consistent && row.ratePct != null ? formatRate(row.ratePct) + "%" : "—"]) {
        const td = cell("td", value); td.className = "numeric";
        if (!summary.consistent) td.title = "对账失败，百分比不可用";
        tr.append(td);
      }
      fragment.append(tr);
    }
    $("#signature-table").replaceChildren(fragment);
  }
  $("#signature-count").textContent = "共 " + formatCount(summary.rows.length) + " 种类型 · 搜索匹配 " + formatCount(matching.length) + " 种 · 已显示 " + formatCount(Math.min(signatureLimit, matching.length)) + " 种（保留全量数据）";
  $("#signature-more").hidden = signatureLimit >= matching.length;
}

function cell(tag, value, scope) {
  const element = document.createElement(tag); element.textContent = value;
  if (scope) element.scope = scope;
  return element;
}
function pointLabel(point) {
  return point.eventDay + "，" + point.group + "，" + (point.ratePct == null ? "占比不可用" : formatRate(point.ratePct) + "%") + "，匹配崩溃设备日 " + formatCount(point.numerator) + "，" + platformLabel(selectedDimension === "mobile_platform" ? [point.group] : undefined) + " 同步设备日 " + formatCount(point.denominator) + "，全平台同步设备日 " + formatCount(point.allSyncedDevices) + "，覆盖 " + point.observedDays + "/" + point.periodDays + " 日，" + statusLabel(point.status);
}
function statusLabel(status) {
  return { PARTIAL_PERIOD: "部分周期", NO_SYNC_SAMPLE: "无已覆盖平台同步样本", NO_ANDROID_SYNC_SAMPLE: "无 Android 同步样本", NO_MATCHED_CRASH_DEVICE: "未匹配到崩溃设备", CRASH_RATE_NOT_COVERED_BY_ANDROID_RULE: "崩溃率未覆盖", HAS_MATCHED_CRASH_DEVICE: "有匹配崩溃设备", NO_DATA_FOR_PERIOD: "无可用观测", RATE_UNAVAILABLE: "占比不可用", OUTSIDE_FIRMWARE_SCOPE: "尚未纳入范围" }[status] || status;
}
function platformLabel(platforms) {
  if (platforms == null) {
    platforms = filters.mobile_platform.length ? filters.mobile_platform : coveredPlatforms;
    if (selectedDimension === "mobile_platform" && cachedView) platforms = platforms.filter((platform) => cachedView.groups.includes(platform) && !hiddenGroups.has(platform));
  }
  const selected = coveredPlatforms.filter((platform) => platforms.includes(platform));
  return selected.map((platform) => platform === "ios" ? "iOS" : "Android").join(" + ") || "已覆盖平台";
}
function queryTime(manifest) {
  if (manifest.queried_at_utc) return manifest.queried_at_utc;
  const times = (manifest.file_metadata || []).map((part) => part.query_ended_at_utc || part.query_started_at_utc).filter(Boolean).sort();
  return times.length ? times[0] + (times[0] !== times.at(-1) ? " 至 " + times.at(-1) : "") : "见快照清单（未提供查询时间）";
}
function orderGroups(groups, field) {
  if (field === "firmware_version" || field === "app_version") return groups.sort((a, b) => b.localeCompare(a, "zh-CN", { numeric: true }));
  if (field === "mobile_platform") return groups.sort((a, b) => a === b ? 0 : a === "android" ? -1 : b === "android" ? 1 : a.localeCompare(b));
  return groups.sort((a, b) => a.localeCompare(b, "zh-CN"));
}
function formatRate(value) { return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(value); }
function formatCount(value) { return value == null ? "—" : value.toLocaleString("zh-CN"); }
function makeSvgElement(tag, attributes = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
  return element;
}
function addText(svg, x, y, text, anchor, className = "") {
  const element = makeSvgElement("text", { x, y, "text-anchor": anchor, class: className }); element.textContent = text; svg.append(element);
}
