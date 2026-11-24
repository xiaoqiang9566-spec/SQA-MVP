import { coveredPlatformSet, filterDimensions, parseCohortRows } from "./data.mjs";

export function parseSignatureRows(rows, { coveredPlatforms = ["android"] } = {}) {
  const covered = coveredPlatformSet(coveredPlatforms);
  const cohorts = parseCohortRows(rows, { coveredPlatforms });
  for (const cohort of cohorts) {
    for (const combination of cohort.combinations) {
      const values = combination.signatures;
      if (!covered.has(combination.mobile_platform) || !combination.has_matched_crash
        || !Array.isArray(values) || values.length === 0
        || values.some((value) => typeof value !== "string" || !value.trim())
        || new Set(values).size !== values.length) {
        throw new Error("崩溃类型队列缺少有效的已覆盖平台同日匹配签名集合。");
      }
    }
  }
  return cohorts;
}

export function buildSignatureSummary(signatureCohorts, syncCohorts, {
  filters = {}, groupField, visibleGroups, startDate = "", endDate = "9999-12-31", coveredPlatforms = ["android"],
} = {}) {
  if (groupField != null && !filterDimensions.includes(groupField)) throw new Error("不支持的类型对比维度。");
  if (visibleGroups != null && !groupField) throw new Error("限定可见组时需要对比维度。");
  const groups = visibleGroups == null ? null : new Set(visibleGroups);
  const activeFilters = Object.entries(filters).map(([field, values]) => [field, (Array.isArray(values) ? values : [values]).filter((value) => value != null && value !== "")]).filter(([, values]) => values.length);
  const covered = coveredPlatformSet(coveredPlatforms);
  const matches = (combination) => covered.has(combination.mobile_platform)
    && (!groups || groups.has(combination[groupField]))
    && activeFilters.every(([field, values]) => values.includes(combination[field]));
  let syncedDeviceDays = 0;
  let matchedDeviceDays = 0;
  for (const cohort of syncCohorts) {
    if (cohort.event_day < startDate || cohort.event_day > endDate) continue;
    let hasSync = false;
    let hasCrash = false;
    for (const combination of cohort.combinations) {
      if (!matches(combination)) continue;
      hasSync = true;
      hasCrash ||= combination.has_matched_crash;
    }
    if (hasSync) syncedDeviceDays += cohort.device_count;
    if (hasCrash) matchedDeviceDays += cohort.device_count;
  }
  const counts = new Map();
  let signatureDeviceDays = 0;
  for (const cohort of signatureCohorts) {
    if (cohort.event_day < startDate || cohort.event_day > endDate) continue;
    const selected = new Set();
    for (const combination of cohort.combinations) {
      if (matches(combination)) for (const signature of combination.signatures) selected.add(signature);
    }
    if (selected.size) signatureDeviceDays += cohort.device_count;
    for (const signature of selected) counts.set(signature, (counts.get(signature) || 0) + cohort.device_count);
  }
  const consistent = signatureDeviceDays === matchedDeviceDays;
  const rows = [...counts].map(([signature, deviceDays]) => ({
    signature,
    recordKind: signature === "[缺少签名]" ? "MISSING_SIGNATURE"
      : /^(=+|Possible Call Backtrace:|Stack range:|Code range:|Assert task:|Assert in(?:\s|:)|#\d+\s+0x[0-9a-f]+\b|0x[0-9a-f]+\s*:)/i.test(signature) ? "LOG_FRAGMENT" : "SIGNATURE_CANDIDATE",
    deviceDays,
    sharePct: consistent && matchedDeviceDays > 0 ? deviceDays / matchedDeviceDays * 100 : null,
    ratePct: consistent && syncedDeviceDays > 0 ? deviceDays / syncedDeviceDays * 100 : null,
  })).sort((a, b) => b.deviceDays - a.deviceDays || a.signature.localeCompare(b.signature, "zh-CN"));
  return { rows, matchedDeviceDays, syncedDeviceDays, signatureDeviceDays, consistent };
}
