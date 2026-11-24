export function parseCsv(text) {
  const source = text.replace(/^\uFEFF/, "");
  const records = [];
  let fields = [];
  let field = "";
  let inQuotes = false;

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];

    if (inQuotes) {
      if (character === '"' && source[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (character === '"') {
        inQuotes = false;
      } else {
        field += character;
      }
      continue;
    }

    if (character === '"' && field.length === 0) {
      inQuotes = true;
    } else if (character === ",") {
      fields.push(field);
      field = "";
    } else if (character === "\n" || character === "\r") {
      if (character === "\r" && source[index + 1] === "\n") index += 1;
      fields.push(field);
      records.push(fields);
      fields = [];
      field = "";
    } else {
      field += character;
    }
  }

  if (inQuotes) throw new Error("CSV ended inside a quoted field");
  if (fields.length > 0 || field.length > 0) {
    fields.push(field);
    records.push(fields);
  }

  const [headers, ...dataRows] = records;
  if (!headers) return [];

  return dataRows
    .filter((row) => row.some((value) => value !== ""))
    .map((row) => Object.fromEntries(headers.map((header, index) => [header, row[index] ?? ""])));
}

export function filterRows(rows, filters = {}) {
  const activeFilters = normalizeFilters(filters);
  return rows.filter((row) => matchesFilters(row, activeFilters));
}

const comparisonFields = ["firmware_version", "user_region", "model_name", "mobile_platform", "app_version"];
export const filterDimensions = comparisonFields;

export function coveredPlatformSet(coveredPlatforms = ["android"]) {
  return new Set(coveredPlatforms.filter((platform) => platform === "android" || platform === "ios"));
}

export function parseCohortRows(rows, { coveredPlatforms = ["android"] } = {}) {
  const covered = coveredPlatformSet(coveredPlatforms);
  const keys = new Set();
  return rows.map((row) => {
    const deviceCount = Number(row.device_count);
    if (!Number.isSafeInteger(deviceCount) || deviceCount <= 0) {
      throw new Error("无效的设备集合计数 device_count。");
    }
    const date = new Date(`${row.event_day}T00:00:00Z`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(row.event_day) || Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== row.event_day) {
      throw new Error("无效的设备集合日期 event_day。");
    }
    const combinations = JSON.parse(row.combinations_json);
    if (!Array.isArray(combinations) || combinations.length === 0) {
      throw new Error("设备集合缺少实际同步组合。");
    }
    for (const combination of combinations) {
      if (!combination || comparisonFields.some((field) => typeof combination[field] !== "string" || combination[field].length === 0)) {
        throw new Error("设备集合的实际组合缺少筛选维度。");
      }
      if (typeof combination.has_matched_crash !== "boolean"
        || (!covered.has(combination.mobile_platform) && combination.has_matched_crash)) {
        throw new Error(covered.has("ios") ? "设备集合缺少有效的 Android / iOS 同日崩溃匹配标记。" : "设备集合缺少有效的同日 Android 崩溃匹配标记。");
      }
    }
    const key = `${row.event_day}\u0000${row.combinations_json}`;
    if (keys.has(key)) throw new Error("存在重复的设备集合。");
    keys.add(key);
    return { event_day: row.event_day, combinations, device_count: deviceCount };
  });
}

export function getFilterOptions(cohorts, { filters = {}, startDate = "", endDate = "9999-12-31" } = {}) {
  const options = Object.fromEntries(comparisonFields.map((field) => [field, new Set()]));
  const activeFilters = normalizeFilters(filters);
  for (const cohort of cohorts) {
    if (cohort.event_day < startDate || cohort.event_day > endDate) continue;
    for (const combination of cohort.combinations) {
      for (const field of comparisonFields) {
        if (activeFilters.every(([filterField, values]) => filterField === field || values.includes(combination[filterField]))) {
          options[field].add(combination[field]);
        }
      }
    }
  }
  return Object.fromEntries(comparisonFields.map((field) => [field, [...options[field]].sort()]));
}

export function buildComparisonDailyRows(cohorts, { filters = {}, groupField = "firmware_version", startDate = "", endDate = "9999-12-31", firmwareStartDays = {}, coveredPlatforms = ["android"], queriedDays } = {}) {
  if (!comparisonFields.includes(groupField)) throw new Error(`不支持的对比维度 ${groupField}。`);
  const activeFilters = normalizeFilters(filters);
  const observations = new Map();
  const groupCoverage = new Map();
  const covered = coveredPlatformSet(coveredPlatforms);
  const queryDays = new Set(queriedDays || []);
  const observedFirmwareStarts = new Map();
  const groupFirmwareVersions = new Map();

  for (const cohort of cohorts) {
    for (const combination of cohort.combinations) {
      const version = combination.firmware_version;
      const firstDay = observedFirmwareStarts.get(version);
      if (!firstDay || cohort.event_day < firstDay) observedFirmwareStarts.set(version, cohort.event_day);
    }
    const inRange = cohort.event_day >= startDate && cohort.event_day <= endDate;
    if (!inRange && queriedDays == null) continue;
    if (inRange && queriedDays == null) queryDays.add(cohort.event_day);
    const matchingGroups = new Map();
    for (const combination of cohort.combinations) {
      if (!matchesFilters(combination, activeFilters)) continue;
      const group = combination[groupField];
      const membership = matchingGroups.get(group) || { android: false, covered: false, crash: false };
      membership.android ||= combination.mobile_platform === "android";
      membership.covered ||= covered.has(combination.mobile_platform);
      membership.crash ||= covered.has(combination.mobile_platform) && combination.has_matched_crash;
      matchingGroups.set(group, membership);
      const versions = groupFirmwareVersions.get(group) || new Set();
      versions.add(combination.firmware_version);
      groupFirmwareVersions.set(group, versions);
    }
    for (const [group, membership] of matchingGroups) {
      groupCoverage.set(group, groupCoverage.get(group) || membership.covered);
      if (!inRange) continue;
      const key = `${cohort.event_day}\u0000${group}`;
      const total = observations.get(key) || { all: 0, android: 0, covered: 0, crash: 0 };
      total.all += cohort.device_count;
      if (membership.android) total.android += cohort.device_count;
      if (membership.covered) total.covered += cohort.device_count;
      if (membership.crash) total.crash += cohort.device_count;
      observations.set(key, total);
    }
  }

  if (queriedDays != null) {
    const selected = activeFilters.find(([field]) => field === groupField)?.[1] || [];
    const platforms = activeFilters.find(([field]) => field === "mobile_platform")?.[1];
    for (const group of selected) {
      if (!groupCoverage.has(group)) groupCoverage.set(group, groupField === "mobile_platform" ? covered.has(group) : !platforms || platforms.some((platform) => covered.has(platform)));
      if (!groupFirmwareVersions.has(group)) groupFirmwareVersions.set(group, new Set(groupField === "firmware_version" ? [group] : filters.firmware_version || []));
    }
  }
  const groups = [...groupCoverage.keys()].sort();
  const groupStartDays = new Map(groups.map((group) => [group, [...groupFirmwareVersions.get(group)]
    .map((version) => firmwareStartDays[version] || observedFirmwareStarts.get(version))
    .filter(Boolean).sort()[0] || startDate]));
  const availableDays = [...queryDays].filter((day) => day >= startDate && day <= endDate).sort();
  if (availableDays.length === 0) return [];
  const firstDay = startDate || availableDays[0];
  const lastDay = endDate === "9999-12-31" ? availableDays.at(-1) : endDate;
  return listPeriodStarts(firstDay, lastDay, "day").flatMap((bucket) => groups.filter((group) => bucket.startDay >= groupStartDays.get(group)).map((group) => {
    const eventDay = bucket.startDay;
    if (!queryDays.has(eventDay)) {
      return {
        event_day: eventDay,
        comparison_value: group,
        synced_devices: null,
        matched_crash_devices: null,
        all_synced_devices: null,
        android_synced_devices: null,
        data_status: "NO_DATA_FOR_DAY",
      };
    }
    const total = observations.get(`${eventDay}\u0000${group}`) || { all: 0, android: 0, covered: 0, crash: 0 };
    const groupCovered = groupCoverage.get(group);
    const status = !groupCovered
      ? "CRASH_RATE_NOT_COVERED_BY_ANDROID_RULE"
      : total.covered === 0 ? (covered.has("ios") ? "NO_SYNC_SAMPLE" : "NO_ANDROID_SYNC_SAMPLE")
        : total.crash === 0 ? "NO_MATCHED_CRASH_DEVICE"
          : "HAS_MATCHED_CRASH_DEVICE";
    return {
      event_day: eventDay,
      comparison_value: group,
      synced_devices: total.covered,
      matched_crash_devices: groupCovered ? total.crash : null,
      all_synced_devices: total.all,
      android_synced_devices: total.android,
      data_status: status,
    };
  }));
}

function normalizeFilters(filters) {
  return Object.entries(filters)
    .map(([field, value]) => [field, Array.isArray(value) ? value : [value]])
    .map(([field, values]) => [field, values.filter((value) => value !== "" && value != null)])
    .filter(([, values]) => values.length > 0);
}

function matchesFilters(row, activeFilters) {
  return activeFilters.every(([field, values]) => values.includes(row[field]));
}

export function getOverviewRegionOptions(rows, firmwareVersion) {
  const regions = [...new Set(rows
    .filter((row) => row.aggregation_level === "region_total" && row.firmware_version === firmwareVersion)
    .map((row) => row.user_region)
    .filter((region) => region))].sort();
  return ["总体", ...(regions.length ? ["全部地区", ...regions] : [])];
}

export function splitAggregationRows(rows) {
  return {
    regionalTotals: rows.filter((row) => row.aggregation_level === "region_total"),
    combinations: rows.filter((row) => row.aggregation_level === "combination"),
  };
}

export function buildDailySeries(rows, { startDate, endDate, groupField, fallbackGroups = [] } = {}) {
  const groups = [...new Set(rows.map((row) => (groupField ? row[groupField] : "selected combination")))].sort();
  if (groups.length === 0) groups.push(...fallbackGroups);
  const observations = new Map();

  for (const row of rows) {
    const group = groupField ? row[groupField] : "selected combination";
    const key = `${row.event_day}\u0000${group}`;
    if (observations.has(key)) throw new Error(`Duplicate daily observation for ${row.event_day} and ${group}`);
    observations.set(key, row);
  }

  const result = [];
  const start = new Date(`${startDate}T00:00:00Z`);
  const end = new Date(`${endDate}T00:00:00Z`);

  for (const day = new Date(start); day <= end; day.setUTCDate(day.getUTCDate() + 1)) {
    const eventDay = day.toISOString().slice(0, 10);
    for (const group of groups) {
      const row = observations.get(`${eventDay}\u0000${group}`);
      if (!row) {
        result.push({ eventDay, group, numerator: null, denominator: null, ratePct: null, status: "NO_DATA_FOR_DAY" });
        continue;
      }

      const numerator = toNullableNumber(row.matched_crash_report_devices);
      const denominator = toNullableNumber(row.synced_devices);
      const platformNotCovered = row.mobile_platform && row.mobile_platform !== "android";
      const explicitlyNotCovered = row.data_status === "CRASH_RATE_NOT_COVERED_BY_ANDROID_RULE";
      const ratePct = platformNotCovered || explicitlyNotCovered || numerator == null || denominator == null || denominator === 0
        ? null
        : (numerator / denominator) * 100;

      result.push({
        eventDay,
        group,
        numerator,
        denominator,
        ratePct,
        status: row.data_status || (ratePct == null ? "RATE_UNAVAILABLE" : "OBSERVED"),
      });
    }
  }

  return result;
}

export function buildPeriodSeries(rows, { period, startDate, endDate, groupField = "comparison_value", fallbackGroups = [] } = {}) {
  const groups = [...new Set([...rows.map((row) => row[groupField]), ...fallbackGroups].filter(Boolean))].sort();
  const periodStarts = listPeriodStarts(startDate, endDate, period);
  const observations = new Map();
  const dailyKeys = new Set();

  for (const row of rows) {
    if (row.event_day < startDate || row.event_day > endDate) continue;
    const dailyKey = `${row.event_day}\u0000${row[groupField]}`;
    if (dailyKeys.has(dailyKey)) throw new Error(`Duplicate daily observation for ${row.event_day} and ${row[groupField]}`);
    dailyKeys.add(dailyKey);
    const bucket = periodForDay(row.event_day, period);
    const key = `${bucket.label}\u0000${row[groupField]}`;
    const daily = observations.get(key) || [];
    daily.push(row);
    observations.set(key, daily);
  }

  return periodStarts.flatMap((bucket) => groups.map((group) => {
    const daily = observations.get(`${bucket.label}\u0000${group}`) || [];
    const selectedStart = startDate > bucket.startDay ? startDate : bucket.startDay;
    const selectedEnd = endDate < bucket.endDay ? endDate : bucket.endDay;
    const coverage = {
      observedDays: daily.filter((row) => row.data_status !== "NO_DATA_FOR_DAY").length,
      periodDays: bucket.dayCount,
      selectedDays: Math.round((new Date(`${selectedEnd}T00:00:00Z`) - new Date(`${selectedStart}T00:00:00Z`)) / 86400000) + 1,
      periodStartDay: bucket.startDay,
      periodEndDay: bucket.endDay,
    };
    if (daily.length === 0) {
      return { eventDay: bucket.label, group, numerator: null, denominator: null, allSyncedDevices: null, ratePct: null, status: "NO_DATA_FOR_PERIOD", sampleDays: 0, eligibleDays: 0, ...coverage };
    }

    const denominatorValues = daily.map((row) => toNullableNumber(row.synced_devices));
    const numeratorValues = daily.map((row) => toNullableNumber(row.matched_crash_devices));
    const allSyncedValues = daily.map((row) => toNullableNumber(row.all_synced_devices));
    const denominator = denominatorValues.some((value) => value == null)
      ? null
      : denominatorValues.reduce((sum, value) => sum + value, 0);
    const numerator = numeratorValues.some((value) => value == null)
      ? null
      : numeratorValues.reduce((sum, value) => sum + value, 0);
    const allSyncedDevices = allSyncedValues.some((value) => value == null)
      ? null
      : allSyncedValues.reduce((sum, value) => sum + value, 0);
    const notCovered = daily.some((row) => row.data_status === "CRASH_RATE_NOT_COVERED_BY_ANDROID_RULE");
    const ratePct = notCovered || numerator == null || denominator == null || denominator === 0
      ? null
      : (numerator / denominator) * 100;
    const partial = daily.length < bucket.dayCount;
    const status = notCovered
      ? "CRASH_RATE_NOT_COVERED_BY_ANDROID_RULE"
      : numerator == null || denominator == null ? "RATE_UNAVAILABLE"
        : denominator === 0 ? (daily.some((row) => row.data_status === "NO_SYNC_SAMPLE") ? "NO_SYNC_SAMPLE" : "NO_ANDROID_SYNC_SAMPLE")
          : ratePct === 0 ? "NO_MATCHED_CRASH_DEVICE"
            : "HAS_MATCHED_CRASH_DEVICE";

    return {
      eventDay: bucket.label,
      group,
      numerator,
      denominator,
      allSyncedDevices,
      ratePct,
      status: partial && ratePct != null ? "PARTIAL_PERIOD" : status,
      ...coverage,
      sampleDays: allSyncedValues.filter((value) => value > 0).length,
      eligibleDays: denominatorValues.filter((value) => value > 0).length,
    };
  }));
}

export function listPeriods({ startDate, endDate, period }) {
  return listPeriodStarts(startDate, endDate, period).map((bucket) => bucket.label);
}

function listPeriodStarts(startDate, endDate, period) {
  const buckets = [];
  const end = new Date(`${endDate}T00:00:00Z`);
  for (let date = new Date(`${startDate}T00:00:00Z`); date <= end;) {
    const bucket = periodForDay(date.toISOString().slice(0, 10), period);
    buckets.push(bucket);
    date = new Date(bucket.nextStart);
  }
  return buckets;
}

function periodForDay(day, period) {
  const date = new Date(`${day}T00:00:00Z`);
  let start;
  let next;
  let label;

  if (period === "day") {
    start = new Date(date);
    next = new Date(date);
    next.setUTCDate(next.getUTCDate() + 1);
    label = day;
  } else if (period === "week") {
    start = new Date(date);
    start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));
    next = new Date(start);
    next.setUTCDate(next.getUTCDate() + 7);
    const thursday = new Date(start);
    thursday.setUTCDate(thursday.getUTCDate() + 3);
    const firstThursday = new Date(Date.UTC(thursday.getUTCFullYear(), 0, 4));
    firstThursday.setUTCDate(firstThursday.getUTCDate() - ((firstThursday.getUTCDay() + 6) % 7) + 3);
    const week = 1 + Math.round((thursday - firstThursday) / 604800000);
    label = `${thursday.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
  } else if (period === "month") {
    start = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1));
    next = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1));
    label = `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}`;
  } else if (period === "quarter") {
    const quarterStartMonth = Math.floor(date.getUTCMonth() / 3) * 3;
    start = new Date(Date.UTC(date.getUTCFullYear(), quarterStartMonth, 1));
    next = new Date(Date.UTC(date.getUTCFullYear(), quarterStartMonth + 3, 1));
    label = `${date.getUTCFullYear()}-Q${Math.floor(date.getUTCMonth() / 3) + 1}`;
  } else {
    throw new Error(`Unsupported period: ${period}`);
  }

  return {
    label,
    startDay: start.toISOString().slice(0, 10),
    endDay: new Date(next.getTime() - 86400000).toISOString().slice(0, 10),
    nextStart: next.toISOString().slice(0, 10),
    dayCount: Math.round((next - start) / 86400000),
  };
}

function toNullableNumber(value) {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
