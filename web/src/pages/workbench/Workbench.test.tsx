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
              { id: "w1", title: "价值对齐综述", year: 2024, venue: "Nature", cited_by: 42, provenance: "openalex" },
              { id: "w2", title: "对齐评估方法", year: 2023, venue: "Science", cited_by: 7, provenance: "crossref" },
            ],
          },
        }}
        currentProjectLabel="论文一"
        favoritedIds={["w1"]}
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
          data: { valid: false, stripped: ["套话一句", "空话一句"], cleaned_text: "清洗后的文稿" },
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
  });
});
