/**
 * 实论 API client（原生 fetch，零第三方依赖，照抄 ../fd-zhesi/web/src/api/client.ts 模式）。
 *
 * - base URL 读 import.meta.env.VITE_SHILUN_API_BASE，默认同源（""）→ 请求发到
 *   window.location.origin 下的 /api/...（部署态由 nginx 反代；联调态可用环境变量
 *   指到本地后端，如 VITE_SHILUN_API_BASE=http://127.0.0.1:8010 npm run build）。
 * - token 存 localStorage["shilun_token"]（Logto PKCE 登录写入，顶栏输入框可兜底）。
 * - 检索/公文/写作三类端点接线期并行开发：网络错误、超时、5xx、404 统一抛
 *   ApiUnavailableError，界面据此渲染「该能力接线中」降级态。
 */

export const API_BASE: string = (() => {
  const raw = import.meta.env?.VITE_SHILUN_API_BASE;
  return typeof raw === "string" && raw.trim() ? raw.trim().replace(/\/+$/, "") : "";
})();

export const TOKEN_KEY = "shilun_token";
const TIMEOUT_MS = 30_000;

// ---- token 存取（非浏览器环境退化为内存 shim，测试可用） ----

const memoryStore = new Map<string, string>();
const shim: Pick<Storage, "getItem" | "setItem" | "removeItem"> = {
  getItem: (k) => memoryStore.get(k) ?? null,
  setItem: (k, v) => void memoryStore.set(k, v),
  removeItem: (k) => void memoryStore.delete(k),
};
function store(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  try {
    if (typeof localStorage !== "undefined") return localStorage;
  } catch {
    /* 隐私模式等场景落到内存 shim */
  }
  return shim;
}
export function getToken(): string {
  return store().getItem(TOKEN_KEY) ?? "";
}
export function setToken(token: string): void {
  if (token) store().setItem(TOKEN_KEY, token);
  else store().removeItem(TOKEN_KEY);
}

// ---- 错误类型 ----

/** 「该能力接线中」：网络失败/超时/5xx/接线期 404（端点尚未落成）。 */
export class ApiUnavailableError extends Error {
  constructor(message = "capability not wired yet") {
    super(message);
    this.name = "ApiUnavailableError";
  }
}

/** 业务错误（4xx 等），message 携带后端 detail。 */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// ---- 请求 ----

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  query?: Record<string, string>;
  /** 检索/公文/写作均接线期端点：404/501 也视为「未接线」而非业务错误 */
  wired?: boolean;
}

/** API_BASE 为同源 "" 时拼相对路径（浏览器 fetch 直接可用，node 测试可直接断言字符串）。 */
function buildUrl(path: string, query?: Record<string, string>): string {
  const qs = new URLSearchParams(query ?? {}).toString();
  return `${API_BASE}${path}${qs ? `?${qs}` : ""}`;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = buildUrl(path, opts.query);

  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const hasBody = opts.body !== undefined;
  if (hasBody) headers["Content-Type"] = "application/json";

  let resp: Response;
  try {
    resp = await fetch(url, {
      method: opts.method ?? (hasBody ? "POST" : "GET"),
      headers,
      body: hasBody ? JSON.stringify(opts.body) : undefined,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch {
    // 网络拒绝 / DNS / CORS / 超时（AbortError/TimeoutError）→ 接线中降级
    throw new ApiUnavailableError();
  }

  if (!resp.ok) {
    const unwired = resp.status >= 500 || resp.status === 501 || (opts.wired && resp.status === 404);
    if (unwired) throw new ApiUnavailableError();
    let detail: unknown = "";
    try {
      detail = (await resp.json())?.detail ?? "";
    } catch {
      /* 无 JSON body（如静态服务器返回的 404 HTML） */
    }
    const message = typeof detail === "string" && detail ? detail : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, message);
  }
  if (resp.status === 204) return undefined as T;
  try {
    return (await resp.json()) as T;
  } catch {
    // 200 但 body 不是 JSON（如静态服务器 SPA 兜底页）→ 视为未接线，降级而非白屏
    throw new ApiUnavailableError();
  }
}

// ---- 契约类型（对接 ../server 契约；接线期防御式可选字段） ----

/** 检索结果出处（服务端 Provenance）。 */
export interface WorkProvenance {
  source: string;
  stable_id: string;
  license: string;
  /** 仅授权通道结果填充：来源通道与授权依据。 */
  authorization?: { channel: string; basis: string } | null;
  /** partial=已知覆盖不完整（中文开放元数据兜底）；主路径不写该字段。 */
  coverage?: "partial" | null;
  /** 只有元数据、没有原文。 */
  metadata_only?: boolean | null;
  /** 本条命中查询里的哪些词（部分覆盖的透明度）。 */
  matched_terms?: string[] | null;
  /** 这些词占查询词项的比例。 */
  coverage_ratio?: number | null;
}

/** 检索结果（POST /api/v1/search/works）。 */
export interface WorkResult {
  work_id: string;
  doi?: string | null;
  title?: string | null;
  year?: number | null;
  venue?: string | null;
  cited_by_count?: number | null;
  provenance?: WorkProvenance | null;
}

export interface SearchResponse {
  results: WorkResult[];
  total?: number | null;
  page?: number;
  per_page?: number;
  reason?: string | null;
  detail?: string | null;
  /** 覆盖度整句提示：中文文献经开放元数据检索时出现。 */
  coverage_note?: string | null;
}

/** 文种（GET /api/gov/doctypes）。 */
export interface DoctypeInfo {
  doctype: string;
  count?: number | null;
  sample_ids?: string[];
}

/** 文种模板（GET /api/gov/template?doctype=）。sections/constraints/samples 接线期形状未定，防御式。 */
export interface GovTemplate {
  sections?: unknown;
  constraints?: unknown;
  samples?: unknown;
  sample_insufficient?: boolean;
}

/** 骨架稿（POST /api/gov/draft）。draft 为开放对象（骨架节 + 占位文案），视图端防御式渲染。 */
export interface GovDraftResponse {
  draft?: unknown;
  template_ref?: unknown;
  sample_ids?: string[];
}

/** 写作项目（POST/GET /api/writing/projects）。列表返回形状接线期未定（数组 / {results} / {projects}）。 */
export interface Project {
  id: string;
  title?: string;
  name?: string;
  created_at?: string;
}

export type CitationFormat = "gbt7714" | "apa";

/** 提纲（POST /api/writing/projects/{id}/outline）。 */
export interface OutlineSection {
  title: string;
  basis?: string[];
  needs_evidence?: boolean;
}

export interface OutlineResponse {
  sections: OutlineSection[];
  engine?: string;
}

/** 校验（POST /api/writing/projects/{id}/validate）。 */
export interface ValidateResponse {
  project_id?: string;
  source?: string;
  version_id?: string | null;
  marker_count?: number;
  /** 服务端历史字段：有效引用的 id 列表（防御式规整，兼容计数/布尔旧形态）。 */
  valid?: unknown;
  stripped?: unknown;
  /** locator 索引的原文——高亮必须落在这份文本上。 */
  text?: string;
  cleaned_text?: string;
  verification?: VerificationReport;
  verification_mode?: VerificationMode;
  /** 待核标注：保留在稿中，附无法核验的原因。 */
  annotations?: VerificationAnnotation[];
}

export type VerifyStatus = "pass" | "block" | "pending_review";
export type VerifyLevel = "resolvable" | "authentic" | "support";
export type VerificationMode = "off" | "shadow" | "enforce";

/** 定位契约（与 server 的 locator.py 同形）：字符区间可高亮，字段锚点用于公文要素。 */
export interface VerificationLocator {
  kind: "citation" | "paragraph" | "char_range" | "document" | "field";
  citation_id?: string | null;
  paragraph_index?: number | null;
  char_start?: number | null;
  char_end?: number | null;
  marker?: string | null;
  field?: string | null;
}

/** 单条判定依据。 */
export interface VerificationEvidence {
  source: string;
  detail?: string;
  fields?: Record<string, unknown>;
  url?: string | null;
}

/** 一条核验结果（一条引用 × 一级判定）。 */
export interface VerificationItem {
  rule_id: string;
  level: VerifyLevel;
  status: VerifyStatus;
  locator: VerificationLocator;
  evidence: VerificationEvidence[];
  reason: string;
}

/** 核验报告（随 validate 响应产出，可机读）。 */
export interface VerificationReport {
  report_version: string;
  mode: "shadow" | "enforce";
  generated_at: string;
  citation_count: number;
  counts: { total: number; passed: number; blocked: number; pending: number };
  items: VerificationItem[];
  blocked_citation_ids: string[];
  pending_citation_ids: string[];
}

/** 待核标注。 */
export interface VerificationAnnotation {
  citation_id: string;
  reason: string;
  rule_ids: string[];
  levels: string[];
}

// ---- 防御式规整 ----

/** 引用条目防御式取文案：string 直出；对象按常见字段拼。 */
export function citationText(c: unknown): string {
  if (typeof c === "string") return c;
  if (c && typeof c === "object") {
    const o = c as Record<string, unknown>;
    const text = [o.title, o.text, o.quote, o.label, o.formatted, o.citation].find(
      (v) => typeof v === "string" && v,
    );
    if (text) return String(text);
    const url = [o.url, o.link, o.doi].find((v) => typeof v === "string" && v);
    if (url) return String(url);
    try {
      return JSON.stringify(c);
    } catch {
      return String(c);
    }
  }
  return String(c ?? "");
}

/** 项目列表防御式规整：接受数组 / {results:[]} / {projects:[]}。 */
export function normalizeProjects(data: unknown): Project[] {
  const raw: unknown = Array.isArray(data)
    ? data
    : data && typeof data === "object"
      ? ((data as Record<string, unknown>).results ?? (data as Record<string, unknown>).projects ?? null)
      : null;
  if (!Array.isArray(raw)) return [];
  const out: Project[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const o = item as Record<string, unknown>;
    const id = o.id ?? o.project_id;
    if (typeof id !== "string" && typeof id !== "number") continue;
    out.push({
      id: String(id),
      title: typeof o.title === "string" ? o.title : undefined,
      name: typeof o.name === "string" ? o.name : undefined,
      created_at: typeof o.created_at === "string" ? o.created_at : undefined,
    });
  }
  return out;
}

/** 引用清单防御式规整：接受 string[] / 对象数组 / {citations|formatted|results|items:[]}。 */
export function normalizeCitations(data: unknown): string[] {
  let raw: unknown = data;
  if (data && typeof data === "object" && !Array.isArray(data)) {
    const o = data as Record<string, unknown>;
    raw =
      o.citations ?? o.formatted ?? o.results ?? o.items ?? o.entries ?? null;
  }
  if (!Array.isArray(raw)) return [];
  return raw.map(citationText);
}

/** 模板节/约束等未知条目 → 展示文本（string 直出；对象取 title/name/label 或 JSON）。 */
export function itemText(v: unknown): string {
  return citationText(v);
}

/** 列表字段防御式规整：sections/constraints/samples 统一成 string[]（对象取主字段）。 */
export function textList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map(itemText);
}

// ---- API 面（对齐 ../server 契约；全部 wired：404/5xx → 接线中降级） ----

export const api = {
  // 检索
  searchWorks: (b: { query?: string; doi?: string; rerank?: boolean }) =>
    request<SearchResponse>("/api/v1/search/works", { method: "POST", body: b, wired: true }),

  // 公文
  listDoctypes: () => request<DoctypeInfo[]>("/api/v1/gov/doctypes", { wired: true }),
  govTemplate: (doctype: string) =>
    request<GovTemplate>("/api/v1/gov/template", { query: { doctype }, wired: true }),
  govDraft: (b: { doctype: string; subject: string; key_points: string[] }) =>
    request<GovDraftResponse>("/api/v1/gov/draft", { method: "POST", body: b, wired: true }),

  // 写作
  createProject: (title: string) =>
    request<Project>("/api/v1/writing/projects", { method: "POST", body: { title }, wired: true }),
  listProjects: () => request<unknown>("/api/v1/writing/projects", { wired: true }),
  // 服务端契约是 CitationIn{kind, ref_id, meta}；这里把检索结果映射成该形状。
  addCitation: (projectId: string, work: WorkResult) =>
    request<unknown>(`/api/v1/writing/projects/${encodeURIComponent(projectId)}/citations`, {
      method: "POST",
      body: {
        kind: "openalex",
        ref_id: work.doi || work.work_id,
        meta: {
          title: work.title ?? null,
          year: work.year ?? null,
          venue: work.venue ?? null,
          source: work.provenance?.source ?? null,
          stable_id: work.provenance?.stable_id ?? work.work_id,
          coverage: work.provenance?.coverage ?? null,
          metadata_only: work.provenance?.metadata_only ?? null,
        },
      },
      wired: true,
    }),
  listCitations: (projectId: string, format: CitationFormat) =>
    request<unknown>(`/api/v1/writing/projects/${encodeURIComponent(projectId)}/citations`, {
      query: { format },
      wired: true,
    }),
  outline: (projectId: string) =>
    request<OutlineResponse>(`/api/v1/writing/projects/${encodeURIComponent(projectId)}/outline`, {
      method: "POST",
      body: {},
      wired: true,
    }),
  validate: (projectId: string, text: string) =>
    request<ValidateResponse>(`/api/v1/writing/projects/${encodeURIComponent(projectId)}/validate`, {
      method: "POST",
      body: { text },
      wired: true,
    }),
};

/** 项目展示名（title 或 name，都缺则退 id）。 */
export function projectLabel(p: Project): string {
  return p.title ?? p.name ?? p.id;
}
