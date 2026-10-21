/** client.ts 冒烟：URL 拼装、token 头、降级（网络挂/5xx/接线期 404）与业务错误（4xx）、防御式规整。 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  API_BASE,
  api,
  ApiError,
  ApiUnavailableError,
  normalizeCitations,
  normalizeProjects,
  setToken,
  textList,
} from "./client";

// --- 测试环境 localStorage stub（node 无 DOM） ---
class LocalStorageStub {
  private m = new Map<string, string>();
  getItem(k: string) {
    return this.m.get(k) ?? null;
  }
  setItem(k: string, v: string) {
    this.m.set(k, v);
  }
  removeItem(k: string) {
    this.m.delete(k);
  }
}

const okJson = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
});
const failJson = (status: number, detail?: unknown) => ({
  ok: false,
  status,
  json: async () => (detail === undefined ? {} : { detail }),
});

beforeEach(() => {
  vi.stubGlobal("localStorage", new LocalStorageStub());
  setToken("ui-tok");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("URL 拼装与 token 头", () => {
  it("GET 带 query：同源 /api 前缀 + ?format=，并带 Bearer token", async () => {
    const fetchMock = vi.fn(async () => okJson([]));
    vi.stubGlobal("fetch", fetchMock);

    await api.listCitations("p1", "apa");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${API_BASE}/api/v1/writing/projects/p1/citations?format=apa`);
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer ui-tok");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
  });

  it("POST 带 JSON body 与 Content-Type（/api/v1/search/works 关键词与 DOI 双模式）", async () => {
    const fetchMock = vi.fn(async () => okJson({ results: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await api.searchWorks({ query: "对齐 综述" });
    await api.searchWorks({ doi: "10.1000/xyz123" });
    await api.searchWorks({ query: "乡村振兴", rerank: true });

    const [u1, i1] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(u1).toBe(`${API_BASE}/api/v1/search/works`);
    expect(i1.method).toBe("POST");
    expect(new Headers(i1.headers).get("content-type")).toBe("application/json");
    expect(JSON.parse(String(i1.body))).toEqual({ query: "对齐 综述" });

    const [, i2] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(JSON.parse(String(i2.body))).toEqual({ doi: "10.1000/xyz123" });

    // 精排是显式开关：勾了才带 rerank，默认请求体不变
    const [, i3] = fetchMock.mock.calls[2] as unknown as [string, RequestInit];
    expect(JSON.parse(String(i3.body))).toEqual({ query: "乡村振兴", rerank: true });
  });

  it("路径参数走 encodeURIComponent；gov/template 的 doctype 走 query", async () => {
    const fetchMock = vi.fn(async () => okJson({}));
    vi.stubGlobal("fetch", fetchMock);

    await api.validate("proj id/1", "贴入文本");
    await api.govTemplate("通知");

    const [u1] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(u1).toBe(`${API_BASE}/api/v1/writing/projects/proj%20id%2F1/validate`);
    const [u2] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(u2).toBe(`${API_BASE}/api/v1/gov/template?doctype=${encodeURIComponent("通知")}`);
  });

  it("未设 token 时不发 Authorization 头", async () => {
    setToken("");
    const fetchMock = vi.fn(async () => okJson([]));
    vi.stubGlobal("fetch", fetchMock);

    await api.listDoctypes();

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).get("authorization")).toBeNull();
  });

  it("token 持久化在 localStorage['shilun_token']", async () => {
    const ls = new LocalStorageStub();
    vi.stubGlobal("localStorage", ls);
    setToken("t2");
    expect(ls.getItem("shilun_token")).toBe("t2");
    setToken("");
    expect(ls.getItem("shilun_token")).toBeNull();
  });
});

describe("降级与错误映射", () => {
  it("fetch reject（网络/超时/拒绝连接）→ ApiUnavailableError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("fetch failed"))));
    await expect(api.searchWorks({ query: "q" })).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.listDoctypes()).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.listProjects()).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("5xx → ApiUnavailableError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => failJson(503, "boom")));
    await expect(api.listProjects()).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("接线期端点 404 → ApiUnavailableError（骨架稿/提纲/校验/检索/清单）", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => failJson(404, "Not Found")));
    await expect(api.searchWorks({ query: "q" })).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.listDoctypes()).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.govTemplate("通知")).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.govDraft({ doctype: "通知", subject: "s", key_points: [] })).rejects.toBeInstanceOf(
      ApiUnavailableError,
    );
    await expect(api.outline("p1")).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.validate("p1", "text")).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.listCitations("p1", "gbt7714")).rejects.toBeInstanceOf(ApiUnavailableError);
  });

  it("4xx 带 detail → ApiError(status, detail)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => failJson(401, "missing bearer token")));
    const err = await api.listProjects().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(401);
    expect(apiErr.message).toContain("missing bearer token");
  });

  it("200 但 body 非 JSON（静态兜底页）→ ApiUnavailableError 降级", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 200, json: async () => Promise.reject(new SyntaxError("Unexpected token <")) })),
    );
    await expect(api.listDoctypes()).rejects.toBeInstanceOf(ApiUnavailableError);
  });
});

describe("防御式规整（接线期返回形状未定）", () => {
  it("normalizeProjects：数组 / {results} / {projects} / 脏数据", () => {
    expect(normalizeProjects([{ id: "a", title: "论文一" }])).toEqual([{ id: "a", title: "论文一", name: undefined, created_at: undefined }]);
    expect(normalizeProjects({ results: [{ project_id: 7, name: "N" }] })).toEqual([
      { id: "7", title: undefined, name: "N", created_at: undefined },
    ]);
    expect(normalizeProjects({ projects: [] })).toEqual([]);
    expect(normalizeProjects({ foo: 1 })).toEqual([]);
    expect(normalizeProjects(null)).toEqual([]);
  });

  it("normalizeCitations：string[] / 对象数组 / {citations|formatted|results}", () => {
    expect(normalizeCitations(["[1] 张三. 文章[J]. 期刊, 2024."])).toEqual(["[1] 张三. 文章[J]. 期刊, 2024."]);
    expect(normalizeCitations([{ title: "T", id: "x" }])).toEqual(["T"]);
    expect(normalizeCitations({ citations: ["a", "b"] })).toEqual(["a", "b"]);
    expect(normalizeCitations({ formatted: ["f"] })).toEqual(["f"]);
    expect(normalizeCitations({ results: [{ text: "r" }] })).toEqual(["r"]);
    expect(normalizeCitations({ nope: true })).toEqual([]);
  });

  it("textList：模板节/约束统一 string[]（对象取主字段）", () => {
    expect(textList(["一、总体要求", { title: "二、主要目标" }])).toEqual(["一、总体要求", "二、主要目标"]);
    expect(textList(undefined)).toEqual([]);
    expect(textList("not-array")).toEqual([]);
  });
});
