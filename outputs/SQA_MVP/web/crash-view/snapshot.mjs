import { parseCsv, parseCohortRows } from "./data.mjs";

const contract = "same_day_android_ios_matched_crash_device_days_v2";
const legacyTrend = "dashboard_filter_cohorts_2026-11-24.manifest.json";
const legacySignature = "dashboard_signature_cohorts_2026-11-24.manifest.json";
const decoder = new TextDecoder("utf-8", { fatal: true });
const hashPattern = /^[a-fA-F0-9]{64}$/;
const validDay = (day) => typeof day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(day) && !Number.isNaN(Date.parse(day)) && new Date(day).toISOString().slice(0, 10) === day;
const canonical = (value) => JSON.stringify(Array.isArray(value) ? value.map((item) => JSON.parse(canonical(item))) : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, JSON.parse(canonical(value[key]))])) : value);

// LAN 设备通常通过普通 HTTP 访问，部分浏览器在非安全上下文中没有 crypto.subtle。
// 保留同一份原始字节哈希校验，并使用小型纯 JS SHA-256 回退，避免要求每台设备安装证书。
function sha256Fallback(bytes) {
  const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
  const h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
  const bitLen = bytes.length * 8, total = (((bytes.length + 9 + 63) >> 6) << 6), data = new Uint8Array(total);
  data.set(bytes); data[bytes.length] = 0x80;
  const view = new DataView(data.buffer); view.setUint32(total - 8, Math.floor(bitLen / 0x100000000)); view.setUint32(total - 4, bitLen >>> 0);
  const w = new Uint32Array(64);
  for (let offset = 0; offset < total; offset += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(offset + i * 4);
    for (let i = 16; i < 64; i++) { const x=w[i-15], y=w[i-2]; const s0=((x>>>7)|(x<<25))^((x>>>18)|(x<<14))^(x>>>3); const s1=((y>>>17)|(y<<15))^((y>>>19)|(y<<13))^(y>>>10); w[i]=(w[i-16]+s0+w[i-7]+s1)>>>0; }
    let [a,b,c,d,e,f,g,hh] = h;
    for (let i = 0; i < 64; i++) { const S1=((e>>>6)|(e<<26))^((e>>>11)|(e<<21))^((e>>>25)|(e<<7)); const ch=(e&f)^(~e&g); const t1=(hh+S1+ch+K[i]+w[i])>>>0; const S0=((a>>>2)|(a<<30))^((a>>>13)|(a<<19))^((a>>>22)|(a<<10)); const maj=(a&b)^(a&c)^(b&c); const t2=(S0+maj)>>>0; [hh,g,f,e,d,c,b,a]=[g,f,e,(d+t1)>>>0,c,b,a,(t1+t2)>>>0]; }
    h[0]=(h[0]+a)>>>0; h[1]=(h[1]+b)>>>0; h[2]=(h[2]+c)>>>0; h[3]=(h[3]+d)>>>0; h[4]=(h[4]+e)>>>0; h[5]=(h[5]+f)>>>0; h[6]=(h[6]+g)>>>0; h[7]=(h[7]+hh)>>>0;
  }
  return h.map((x) => x.toString(16).padStart(8, "0")).join("");
}

function basename(file, extension) {
  if (typeof file !== "string" || !/^[a-zA-Z0-9][\w.-]*$/.test(file) || file.includes("..") || !file.endsWith(extension)) throw new Error("快照文件名无效，必须是合法 basename：" + String(file));
  return file;
}

function validateFiles(manifest, strict, requireHashes = false) {
  if (!Array.isArray(manifest?.files) || !manifest.files.length || new Set(manifest.files).size !== manifest.files.length) throw new Error("快照清单缺少完整且唯一的文件列表。");
  for (const file of manifest.files) basename(file, ".csv");
  if (!strict) return;
  if (!Array.isArray(manifest.file_metadata) || manifest.file_metadata.length !== manifest.files.length) throw new Error("快照缺少完整文件元数据。");
  for (const file of manifest.files) {
    const matches = manifest.file_metadata.filter((item) => item?.file === file);
    if (matches.length !== 1 || !Number.isSafeInteger(matches[0].row_count) || matches[0].row_count < 0) throw new Error(file + " 缺少有效的完整记录数。");
    if ((requireHashes || matches[0].sha256 != null) && !hashPattern.test(matches[0].sha256)) throw new Error(file + " 缺少有效的 SHA-256。");
  }
}

function validateV2(manifest, pointer) {
  if (manifest?.schema_version !== 2 || manifest.snapshot_id !== pointer.snapshot_id) throw new Error("清单 schema 或 snapshot_id 代次不一致。");
  if (manifest.metric_contract !== contract) throw new Error("清单统计口径不一致。");
  if (!Array.isArray(manifest.covered_platforms) || manifest.covered_platforms.length !== 2 || [...manifest.covered_platforms].sort().join(",") !== "android,ios") throw new Error("清单覆盖平台不一致。");
  if (manifest.validation?.status !== "VERIFIED") throw new Error("快照尚未通过独立数据核验。");
  if (!validDay(manifest.start_date) || !validDay(manifest.end_date) || manifest.start_date > manifest.end_date) throw new Error("清单日期无效。");
  const days = manifest.queried_days;
  if (!Array.isArray(days) || days.length !== Math.round((Date.parse(manifest.end_date) - Date.parse(manifest.start_date)) / 86400000) + 1
    || days.some((day, index) => !validDay(day) || day !== new Date(Date.parse(manifest.start_date) + index * 86400000).toISOString().slice(0, 10))) throw new Error("清单成功查询日期不完整或不连续。");
  if (!Array.isArray(manifest.firmware_scope) || manifest.firmware_scope.some((entry) => typeof entry?.firmware_version !== "string" || !entry.firmware_version || !validDay(entry.scope_start_utc))
    || new Set(manifest.firmware_scope.map((entry) => entry.firmware_version)).size !== manifest.firmware_scope.length) throw new Error("清单固件范围无效。");
  validateFiles(manifest, true, true);
}

// 启动只读取一次 latest；闭包固定清单及文件，类型重试也不会切到另一代。
export async function openSnapshot({ fetchImpl = globalThis.fetch, cryptoImpl = globalThis.crypto } = {}) {
  const readBytes = async (file) => {
    const response = await fetchImpl("/api/crash-data/" + file, { cache: "no-store" });
    if (!response.ok) throw new Error("无法读取 " + file + "（HTTP " + response.status + "）。");
    return new Uint8Array(await response.arrayBuffer());
  };
  const verifyHash = async (bytes, expected, file) => {
    if (!hashPattern.test(expected)) throw new Error(file + " SHA-256 无效。");
    const actual = cryptoImpl?.subtle
      ? [...new Uint8Array(await cryptoImpl.subtle.digest("SHA-256", bytes))].map((byte) => byte.toString(16).padStart(2, "0")).join("")
      : sha256Fallback(bytes);
    if (actual !== expected.toLowerCase()) throw new Error(file + " SHA-256 哈希不一致。");
  };
  const latest = await fetchImpl("/api/crash-data/dashboard_latest.json", { cache: "no-store" });
  let pointer = null, manifest, signatureManifest, trendBytes, trendFile = legacyTrend, signatureFile = legacySignature;
  if (latest.status === 404) {
    manifest = JSON.parse(decoder.decode(await readBytes(legacyTrend)));
    if (manifest?.schema_version != null && manifest.schema_version !== 1) throw new Error("历史入口仅支持 v1 快照。");
    validateFiles(manifest, false);
  } else {
    if (!latest.ok) throw new Error("无法读取最新快照指针（HTTP " + latest.status + "）。");
    pointer = JSON.parse(decoder.decode(new Uint8Array(await latest.arrayBuffer())));
    if (pointer?.schema_version !== 1 || typeof pointer.snapshot_id !== "string" || !/^[\w][\w.-]*$/.test(pointer.snapshot_id)
      || !hashPattern.test(pointer.trend_sha256) || !hashPattern.test(pointer.signature_sha256)
      || typeof pointer.published_at_utc !== "string" || !/(Z|\+00:00)$/.test(pointer.published_at_utc) || Number.isNaN(Date.parse(pointer.published_at_utc))) throw new Error("最新快照指针无效。");
    trendFile = basename(pointer.trend_manifest, ".json");
    signatureFile = basename(pointer.signature_manifest, ".json");
    if (trendFile === signatureFile) throw new Error("趋势与类型清单文件不应相同。");
    trendBytes = await readBytes(trendFile);
    await verifyHash(trendBytes, pointer.trend_sha256, trendFile);
    manifest = JSON.parse(decoder.decode(trendBytes));
    validateV2(manifest, pointer);
  }
  const coveredPlatforms = pointer ? ["android", "ios"] : ["android"];
  const loadRows = async (source, signature = false, signatureParser) => {
    validateFiles(source, Boolean(pointer) || signature, Boolean(pointer));
    const parts = await Promise.all(source.files.map(async (file) => {
      const bytes = await readBytes(file);
      const metadata = source.file_metadata?.find((entry) => entry.file === file);
      if (pointer) await verifyHash(bytes, metadata.sha256, file);
      const rows = parseCsv(decoder.decode(bytes));
      if (metadata?.row_count != null && rows.length !== metadata.row_count) throw new Error(file + " 记录数与完整快照不一致。");
      return rows;
    }));
    const parseRows = signature ? signatureParser || (await import("./signatures.mjs")).parseSignatureRows : parseCohortRows;
    const rows = parseRows(parts.flat(), { coveredPlatforms });
    if (rows.some((row) => row.event_day < manifest.start_date || row.event_day > manifest.end_date)) throw new Error("观测日期超出快照范围。");
    if (!signature && !pointer) {
      const dates = [...new Set(rows.map((row) => row.event_day))].sort();
      if (!rows.length) throw new Error("快照没有实际观测。");
      if (dates[0] !== manifest.start_date || dates.at(-1) !== manifest.end_date) throw new Error("快照日期与清单不一致。");
    }
    return rows;
  };
  return {
    pointer, manifest, coveredPlatforms,
    get signatureManifest() { return signatureManifest; },
    loadTrendRows: () => loadRows(manifest),
    loadSignatureRows: async (parseRows) => {
      // 每次尝试重新读取固定指针指定的类型清单；失败只影响类型，不重读 latest。
      const candidateBytes = await readBytes(signatureFile);
      if (pointer) await verifyHash(candidateBytes, pointer.signature_sha256, signatureFile);
      const candidate = JSON.parse(decoder.decode(candidateBytes));
      if (pointer) {
        validateV2(candidate, pointer);
        for (const field of ["start_date", "end_date", "queried_days", "firmware_scope"]) {
          if (canonical(manifest[field]) !== canonical(candidate[field])) throw new Error("类型与趋势清单 " + field + " 不一致。");
        }
        if (candidate.parent_snapshot_file !== trendFile) throw new Error("类型父快照文件不一致。");
        await verifyHash(trendBytes, candidate.parent_snapshot_sha256, "类型父快照");
      } else {
        if (candidate?.schema_version != null && candidate.schema_version !== 1) throw new Error("历史类型入口仅支持 v1 快照。");
        if (candidate?.validation?.status !== "VERIFIED") throw new Error("类型快照尚未通过独立数据核验，暂不可用。");
        if (candidate.start_date !== manifest.start_date || candidate.end_date !== manifest.end_date) throw new Error("类型与趋势快照日期范围不一致。");
      }
      const rows = await loadRows(candidate, true, parseRows);
      signatureManifest = candidate;
      return rows;
    },
  };
}
