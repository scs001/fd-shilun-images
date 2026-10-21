/**
 * Workbench 冒烟：API 全挂（mock fetch reject）时三个 tab 的降级渲染。
 * 无 jsdom 依赖，用 react-dom/server renderToString 直渲视图层
 * （视图与容器同文件，容器 useEffect 走的就是同一 mapApiError 映射，
 *  client.test.ts 已覆盖 fetch reject → ApiUnavailableError）。
 */
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import "../../i18n";
import { api, ApiUnavailableError } from "../../api/client";
import { mapApiError } from "../../api/state";
import { SearchView } from "./SearchPanel";
import { GovView } from "./GovPanel";
import { WritingView } from "./WritingPanel";

const noop = () => {};

// 核验报告用固定文稿：locator 的字符区间必须真的索引到这段文本，
// 否则「按 locator 高亮」只是个装饰。
const VERIFY_TEXT = "第一段：结论与依据一一对应。\n第二段：这里有 [[cite:c1]]、[[cite:c2]] 与 [[cite:c3]] 三处引用。";
const span = (marker: string) => {
  const start = VERIFY_TEXT.indexOf(marker);
  if (start < 0) throw new Error(`测试文稿里没有 ${marker}`);
  return { char_start: start, char_end: start + marker.length };
};

describe("API 全挂时的降级态", () => {
  it("mock fetch reject：面板实际调用的加载路径映射为 unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("fetch failed"))));
    // 这些正是三个容器 useEffect / 提交动作里跑的调用
    await expect(api.listDoctypes()).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.listProjects()).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.listCitations("p1", "gbt7714")).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.searchWorks({ query: "q" })).rejects.toBeInstanceOf(ApiUnavailableError);
    await expect(api.searchWorks({ doi: "10.1/x" })).rejects.toBeInstanceOf(ApiUnavailableError);
    // 容器 catch 后套用的映射
    expect(mapApiError(new ApiUnavailableError())).toEqual({ status: "unavailable" });
    vi.unstubAllGlobals();
  });

  it("检索：结果 unavailable → 渲染「接线中」而非白屏，检索入口仍在", () => {
    const html = renderToString(
      <SearchView
        mode="query"
        onMode={noop}
        text=""
        onText={noop}
        onSubmit={noop}
        busy={false}
        result={{ status: "unavailable" }}
        currentProjectLabel={null}
        favoritedIds={[]}
        favoriteBusyId={null}
        favoriteNote={null}
        onFavorite={noop}
      />,
    );
    expect(html).toContain("接线中");
    expect(html).toContain("检索");
    // 未选项目时给出收藏引导
    expect(html).toContain("先在「写作」");
  });

  it("检索：空结果带原因展示；就绪结果渲染题名/被引/来源与收藏按钮", () => {
    const empty = renderToString(
      <SearchView
        mode="doi"
        onMode={noop}
        text="10.1/x"
        onText={noop}
        onSubmit={noop}
        busy={false}
        result={{ status: "ready", data: { results: [], reason: "DOI 无命中" } }}
        currentProjectLabel="论文一"
        favoritedIds={[]}
        favoriteBusyId={null}
        favoriteNote={null}
        onFavorite={noop}
      />,
    );
    expect(empty).toContain("没有命中的文献");
    expect(empty).toContain("原因：DOI 无命中");

    const ready = renderToString(
      <SearchView
        mode="query"
        onMode={noop}
        text="q"
        onText={noop}
        onSubmit={noop}
        busy={false}
        result={{
          status: "ready",
          data: {
            results: [
              {
                work_id: "W1",
                title: "价值对齐综述",
                year: 2024,
                venue: "Nature",
                cited_by_count: 42,
                provenance: { source: "openalex", stable_id: "W1", license: "CC0" },
              },
              {
                work_id: "10.1/w2",
                title: "对齐评估方法",
                year: 2023,
                venue: "Science",
                cited_by_count: 7,
                provenance: { source: "crossref", stable_id: "10.1/w2", license: "metadata-only" },
              },
            ],
          },
        }}
        currentProjectLabel="论文一"
        favoritedIds={["W1"]}
        favoriteBusyId={null}
        favoriteNote={null}
        onFavorite={noop}
      />,
    );
    expect(ready).toContain("价值对齐综述");
    expect(ready).toContain("Nature");
    expect(ready).toContain("openalex");
    expect(ready).toContain("已收藏"); // 已收藏过的按钮态
    expect(ready).toContain("收藏到当前项目"); // 未收藏的入口仍在（其它结果）
  });

  it("检索：中文开放元数据兜底（coverage=partial）渲染覆盖不完整提示", () => {
    const note =
      "中文文献经开放元数据（Crossref/OpenAlex）检索，覆盖不完整：仅含已注册 DOI 或已被收录的条目。";
    const html = renderToString(
      <SearchView
        mode="query"
        onMode={noop}
        text="乡村振兴"
        onText={noop}
        onSubmit={noop}
        busy={false}
        result={{
          status: "ready",
          data: {
            coverage_note: note,
            results: [
              {
                work_id: "10.1234/cn.2023.001",
                title: "乡村振兴战略下的县域治理",
                year: 2023,
                venue: "中国行政管理",
                provenance: {
                  source: "crossref",
                  stable_id: "10.1234/cn.2023.001",
                  license: "metadata-only",
                  coverage: "partial",
                  metadata_only: true,
                  matched_terms: ["乡村", "振兴", "村振"],
                  coverage_ratio: 0.6,
                },
              },
            ],
          },
        }}
        currentProjectLabel="论文一"
        favoritedIds={[]}
        favoriteBusyId={null}
        favoriteNote={null}
        onFavorite={noop}
      />,
    );
    expect(html).toContain("覆盖不完整");
    expect(html).toContain("乡村振兴战略下的县域治理");
    expect(html).toContain("crossref");
    // 部分覆盖摊开：逐条显示本条覆盖了查询的哪些词
    expect(html).toContain("本条覆盖查询词");
    expect(html).toContain("乡村、振兴、村振");
  });

  it("检索：授权通道命中不渲染覆盖提示", () => {
    const html = renderToString(
      <SearchView
        mode="query"
        onMode={noop}
        text="乡村振兴"
        onText={noop}
        onSubmit={noop}
        busy={false}
        result={{
          status: "ready",
          data: {
            results: [
              {
                work_id: "CN-001",
                title: "乡村振兴战略下的县域治理",
                year: 2023,
                provenance: {
                  source: "wanfang",
                  stable_id: "CN-001",
                  license: "万方合同 W-2026-001",
                  authorization: { channel: "wanfang", basis: "万方合同 W-2026-001" },
                },
              },
            ],
          },
        }}
        currentProjectLabel={null}
        favoritedIds={[]}
        favoriteBusyId={null}
        favoriteNote={null}
        onFavorite={noop}
      />,
    );
    expect(html).not.toContain("覆盖不完整");
    expect(html).toContain("wanfang");
  });

  it("检索：精排开关可选出场——传 onRefine 才渲染勾选框", () => {
    const base = {
      mode: "query" as const,
      onMode: noop,
      text: "乡村振兴",
      onText: noop,
      onSubmit: noop,
      busy: false,
      result: { status: "idle" as const },
      currentProjectLabel: null,
      favoritedIds: [],
      favoriteBusyId: null,
      favoriteNote: null,
      onFavorite: noop,
    };
    const withSwitch = renderToString(<SearchView {...base} refine={false} onRefine={noop} />);
    expect(withSwitch).toContain("精排");
    expect(withSwitch).toContain("用模型复核相关性");
    // 只读渲染（不传 onRefine）保持原样：不出开关，也不能被误当成已开通能力
    const withoutSwitch = renderToString(<SearchView {...base} />);
    expect(withoutSwitch).not.toContain("精排");
  });

  it("公文：doctypes unavailable → 渲染「接线中」", () => {
    const html = renderToString(
      <GovView
        doctypes={{ status: "unavailable" }}
        doctype=""
        onDoctype={noop}
        onReloadDoctypes={noop}
        template={{ status: "idle" }}
        subject=""
        onSubject={noop}
        keyPoints=""
        onKeyPoints={noop}
        onDraft={noop}
        draft={{ status: "idle" }}
        busy={false}
      />,
    );
    expect(html).toContain("接线中");
  });

  it("公文：doctypes 就绪渲染文种计数；模板就绪渲染节/约束/样例计数与样例不足提示", () => {
    const html = renderToString(
      <GovView
        doctypes={{ status: "ready", data: [{ doctype: "通知", count: 12, sample_ids: ["s1", "s2"] }] }}
        doctype="通知"
        onDoctype={noop}
        onReloadDoctypes={noop}
        template={{
          status: "ready",
          data: {
            sections: ["一、总体要求", { title: "二、主要目标" }],
            constraints: ["篇幅 800 字内"],
            samples: [{ id: "s1", title: "某通知" }, { id: "s2", title: "另一通知" }],
            sample_insufficient: true,
          },
        }}
        subject="关于推进×工作的通知"
        onSubject={noop}
        keyPoints="要点一"
        onKeyPoints={noop}
        onDraft={noop}
        draft={{
          status: "ready",
          data: {
            draft: { sections: [{ title: "一、总体要求", content: "……" }] },
            template_ref: "notice/v3",
            sample_ids: ["s1", "s2"],
          },
        }}
        busy={false}
      />,
    );
    expect(html).toContain("12 篇样例");
    expect(html).toContain("一、总体要求");
    expect(html).toContain("二、主要目标");
    expect(html).toContain("篇幅 800 字内");
    expect(html).toContain("样例（2 篇");
    expect(html).toContain("样例不足");
    expect(html).toContain("模板：notice/v3");
    expect(html).toContain("样例可溯：s1、s2");
  });

  it("写作：项目/引用清单/提纲/校验 unavailable → 渲染「接线中」", () => {
    const html = renderToString(
      <WritingView
        projects={{ status: "unavailable" }}
        currentProjectId={null}
        onSelectProject={noop}
        onReloadProjects={noop}
        newName=""
        onNewName={noop}
        onCreate={noop}
        creating={false}
        createNote={null}
        citations={{ status: "unavailable" }}
        format="gbt7714"
        onFormat={noop}
        onCopy={noop}
        copiedIndex={null}
        copyFailed={false}
        outline={{ status: "unavailable" }}
        onOutline={noop}
        outlining={false}
        validateText=""
        onValidateText={noop}
        onValidate={noop}
        validating={false}
        validateResult={{ status: "unavailable" }}
      />,
    );
    expect(html).toContain("接线中");
  });

  it("写作：就绪态渲染两种格式切换、提纲依据徽标与需补证黄标、校验结果", () => {
    const html = renderToString(
      <WritingView
        projects={{ status: "ready", data: [{ id: "p1", title: "论文一" }] }}
        currentProjectId="p1"
        onSelectProject={noop}
        onReloadProjects={noop}
        newName=""
        onNewName={noop}
        onCreate={noop}
        creating={false}
        createNote={null}
        citations={{
          status: "ready",
          data: ["[1] 张三. 文章[J]. 期刊, 2024.", "Author (2024). Article. Journal."],
        }}
        format="gbt7714"
        onFormat={noop}
        onCopy={noop}
        copiedIndex={null}
        copyFailed={false}
        outline={{
          status: "ready",
          data: {
            engine: "llm-x",
            sections: [
              { title: "引言", basis: ["c1"], needs_evidence: false },
              { title: "方法", needs_evidence: true },
            ],
          },
        }}
        onOutline={noop}
        outlining={false}
        validateText="贴入的文稿"
        onValidateText={noop}
        onValidate={noop}
        validating={false}
        validateResult={{
          status: "ready",
          data: {
            text: VERIFY_TEXT,
            valid: ["c1"],
            stripped: ["套话一句", "空话一句"],
            cleaned_text: "清洗后的文稿",
            verification_mode: "shadow",
            verification: {
              report_version: "cite-verify-v1",
              mode: "shadow",
              generated_at: "2026-10-21T12:00:00Z",
              citation_count: 3,
              counts: { total: 3, passed: 1, blocked: 1, pending: 1 },
              items: [
                {
                  rule_id: "cite.resolvable",
                  level: "resolvable",
                  status: "pass",
                  locator: { kind: "citation", citation_id: "c1", ...span("[[cite:c1]]") },
                  evidence: [{ source: "collection", detail: "在收藏清单中" }],
                  reason: "引用可解",
                },
                {
                  rule_id: "cite.metadata",
                  level: "authentic",
                  status: "block",
                  locator: { kind: "citation", citation_id: "c2", ...span("[[cite:c2]]") },
                  evidence: [{ source: "crossref", detail: "年份不一致" }],
                  reason: "元数据不符",
                },
                {
                  rule_id: "cite.fulltext_missing",
                  level: "support",
                  status: "pending_review",
                  locator: { kind: "citation", citation_id: "c3", ...span("[[cite:c3]]") },
                  evidence: [],
                  reason: "无全文可核",
                },
              ],
              blocked_citation_ids: ["c2"],
              pending_citation_ids: ["c3"],
            },
            annotations: [
              { citation_id: "c3", reason: "无全文可核", rule_ids: ["cite.fulltext_missing"], levels: ["support"] },
            ],
          },
        }}
      />,
    );
    expect(html).toContain("GB/T 7714");
    expect(html).toContain("APA");
    expect(html).toContain("[1] 张三. 文章[J]. 期刊, 2024.");
    expect(html).toContain("复制");
    expect(html).toContain("依据 c1"); // 提纲依据 citation 徽标
    expect(html).toContain("需补证"); // needs_evidence 黄标
    expect(html).toContain("引擎：llm-x");
    expect(html).toContain("未通过");
    expect(html).toContain("套话一句");
    expect(html).toContain("清洗后的文稿");
    // 核验报告（task 1.6）：三态汇总 + 逐条定位 + 原文高亮锚点
    expect(html).toContain("通过 1");
    expect(html).toContain("拦截 1");
    expect(html).toContain("待核 1");
    expect(html).toContain("cite.metadata"); // rule_id
    expect(html).toContain("元数据不符");
    expect(html).toContain("Shadow 模式"); // 当前生效模式
    expect(html).toContain("cite-verify-v1"); // 报告版本，可机读契约
    // locator 落到原文段落：段锚点 + 拦截/待核两态高亮
    expect(html).toContain('id="verify-para-1"');
    expect(html).toContain("<mark");
  });
});
