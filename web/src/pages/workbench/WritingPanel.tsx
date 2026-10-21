/** 写作 · 引证成稿：项目选择/新建 → 引用清单（GB/T 7714|APA 切换复制）→ 提纲（依据徽标/需补证黄标）→ 文稿校验。 */
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  normalizeCitations,
  normalizeProjects,
  projectLabel,
  type CitationFormat,
  type OutlineResponse,
  type Project,
  type ValidateResponse,
  type VerificationItem,
  type VerifyStatus,
} from "../../api/client";
import { Loadable, mapApiError } from "../../api/state";
import {
  CitationBadge,
  DegradeBox,
  EmptyLine,
  ErrorBox,
  Loading,
  Note,
  SectionTitle,
  VerifyStatusBadge,
  WarnBadge,
} from "../../components/ui";

// ---- 视图（显式状态，可独立渲染降级态） ----

export function WritingView({
  projects,
  currentProjectId,
  onSelectProject,
  onReloadProjects,
  newName,
  onNewName,
  onCreate,
  creating,
  createNote,
  citations,
  format,
  onFormat,
  onCopy,
  copiedIndex,
  copyFailed,
  outline,
  onOutline,
  outlining,
  validateText,
  onValidateText,
  onValidate,
  validating,
  validateResult,
}: {
  projects: Loadable<Project[]>;
  currentProjectId: string | null;
  onSelectProject: (id: string) => void;
  onReloadProjects: () => void;
  newName: string;
  onNewName: (v: string) => void;
  onCreate: () => void;
  creating: boolean;
  createNote: string | null;
  citations: Loadable<string[]>;
  format: CitationFormat;
  onFormat: (f: CitationFormat) => void;
  onCopy: (text: string, index: number) => void;
  copiedIndex: number | null;
  copyFailed: boolean;
  outline: Loadable<OutlineResponse>;
  onOutline: () => void;
  outlining: boolean;
  validateText: string;
  onValidateText: (v: string) => void;
  onValidate: () => void;
  validating: boolean;
  validateResult: Loadable<ValidateResponse>;
}) {
  const { t } = useTranslation();
  const hasProject = Boolean(currentProjectId);
  return (
    <section className="space-y-6">
      <div>
        <SectionTitle>{t("workbench.writing.heading")}</SectionTitle>
        <p className="mt-1 text-sm text-neutral-500">{t("workbench.writing.hint")}</p>
      </div>

      {/* 项目选择 / 新建 */}
      <div>
        <div className="flex items-baseline justify-between gap-3">
          <SectionTitle>{t("workbench.writing.projects")}</SectionTitle>
          <button
            type="button"
            onClick={onReloadProjects}
            className="text-xs text-neutral-500 underline hover:text-neutral-900"
          >
            {t("workbench.retry")}
          </button>
        </div>
        {projects.status === "loading" ? <Loading /> : null}
        {projects.status === "unavailable" ? (
          <div className="mt-2">
            <DegradeBox />
          </div>
        ) : null}
        {projects.status === "error" ? (
          <div className="mt-2">
            <ErrorBox message={projects.message} code={projects.code} />
          </div>
        ) : null}
        {projects.status === "ready" ? (
          projects.data.length === 0 ? (
            <EmptyLine>{t("workbench.writing.projectsEmpty")}</EmptyLine>
          ) : (
            <ul className="mt-3 space-y-2">
              {projects.data.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => onSelectProject(p.id)}
                    className={`w-full rounded-lg border bg-white p-4 text-left transition-colors ${
                      currentProjectId === p.id ? "border-neutral-900" : "border-neutral-200 hover:border-neutral-400"
                    }`}
                  >
                    <p className="font-serif text-neutral-900">{projectLabel(p)}</p>
                    <p className="mt-0.5 text-xs text-neutral-400">{p.id}</p>
                  </button>
                </li>
              ))}
            </ul>
          )
        ) : null}
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <input
            value={newName}
            onChange={(e) => onNewName(e.target.value)}
            placeholder={t("workbench.writing.newName")}
            className="min-w-0 flex-1 rounded border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
          <button
            type="button"
            disabled={creating || !newName.trim()}
            onClick={onCreate}
            className="rounded border border-neutral-900 px-4 py-2 text-sm text-neutral-900 hover:bg-neutral-900 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            {creating ? t("workbench.writing.creating") : t("workbench.writing.create")}
          </button>
        </div>
        {createNote ? <Note>{createNote}</Note> : null}
      </div>

      {/* 引用清单 */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <SectionTitle>{t("workbench.writing.citations")}</SectionTitle>
          <div className="flex overflow-hidden rounded border border-neutral-300 text-xs" role="group" aria-label={t("workbench.writing.citations")}>
            {(["gbt7714", "apa"] as const).map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => onFormat(f)}
                className={`px-3 py-1 ${
                  format === f ? "bg-neutral-900 text-white" : "bg-white text-neutral-500 hover:bg-neutral-100"
                }`}
              >
                {t(`workbench.writing.format.${f}`)}
              </button>
            ))}
          </div>
        </div>
        {!hasProject ? (
          <p className="mt-2 text-xs text-neutral-400">{t("workbench.project.hint")}</p>
        ) : null}
        {citations.status === "loading" ? <Loading /> : null}
        {citations.status === "unavailable" ? (
          <div className="mt-2">
            <DegradeBox />
          </div>
        ) : null}
        {citations.status === "error" ? (
          <div className="mt-2">
            <ErrorBox message={citations.message} code={citations.code} />
          </div>
        ) : null}
        {citations.status === "ready" ? (
          citations.data.length === 0 ? (
            <EmptyLine>{t("workbench.writing.citationsEmpty")}</EmptyLine>
          ) : (
            <ul className="mt-3 space-y-2">
              {citations.data.map((c, i) => (
                <li key={i} className="flex items-start justify-between gap-3 rounded border border-neutral-100 px-3 py-2">
                  <p className="min-w-0 break-words text-sm text-neutral-700">[{i + 1}] {c}</p>
                  <button
                    type="button"
                    onClick={() => onCopy(c, i)}
                    className={`shrink-0 rounded border px-2 py-0.5 text-xs transition-colors ${
                      copiedIndex === i
                        ? "border-green-300 bg-green-50 text-green-700"
                        : "border-neutral-300 text-neutral-500 hover:border-neutral-900 hover:text-neutral-900"
                    }`}
                  >
                    {copiedIndex === i ? t("workbench.writing.copied") : t("workbench.writing.copy")}
                  </button>
                </li>
              ))}
            </ul>
          )
        ) : null}
        {copyFailed ? <p className="mt-2 text-xs text-red-600">{t("workbench.writing.copyFailed")}</p> : null}
      </div>

      {/* 提纲 */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <div className="flex items-center justify-between gap-3">
          <SectionTitle>{t("workbench.writing.outline")}</SectionTitle>
          <button
            type="button"
            disabled={!hasProject || outlining}
            onClick={onOutline}
            className="rounded border border-neutral-900 px-3 py-1 text-xs text-neutral-900 hover:bg-neutral-900 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            {outlining ? t("workbench.writing.outlining") : t("workbench.writing.outline")}
          </button>
        </div>
        {!hasProject ? (
          <p className="mt-2 text-xs text-neutral-400">{t("workbench.project.hint")}</p>
        ) : null}
        {outline.status === "loading" ? <Loading /> : null}
        {outline.status === "unavailable" ? (
          <div className="mt-2">
            <DegradeBox />
          </div>
        ) : null}
        {outline.status === "error" ? (
          <div className="mt-2">
            <ErrorBox message={outline.message} code={outline.code} />
          </div>
        ) : null}
        {outline.status === "ready" ? <OutlineView data={outline.data} /> : null}
      </div>

      {/* 校验 */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <SectionTitle>{t("workbench.writing.validateHeading")}</SectionTitle>
        <textarea
          value={validateText}
          onChange={(e) => onValidateText(e.target.value)}
          placeholder={t("workbench.writing.validatePlaceholder")}
          rows={5}
          className="mt-3 w-full rounded border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
        />
        <button
          type="button"
          disabled={!hasProject || validating || !validateText.trim()}
          onClick={onValidate}
          className="mt-3 rounded bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {validating ? t("workbench.writing.validating") : t("workbench.writing.validate")}
        </button>
        {validateResult.status === "loading" ? <Loading /> : null}
        {validateResult.status === "unavailable" ? (
          <div className="mt-3">
            <DegradeBox />
          </div>
        ) : null}
        {validateResult.status === "error" ? (
          <div className="mt-3">
            <ErrorBox message={validateResult.message} code={validateResult.code} />
          </div>
        ) : null}
        {validateResult.status === "ready" ? <ValidateResultView data={validateResult.data} onCopy={onCopy} /> : null}
      </div>
    </section>
  );
}

function OutlineView({ data }: { data: OutlineResponse }) {
  const { t } = useTranslation();
  const sections = Array.isArray(data.sections) ? data.sections : [];
  return (
    <div className="mt-3">
      {data.engine ? (
        <p className="text-xs text-neutral-400">{t("workbench.writing.engine", { engine: data.engine })}</p>
      ) : null}
      {sections.length === 0 ? (
        <EmptyLine>{t("workbench.writing.outlineEmpty")}</EmptyLine>
      ) : (
        <ol className="mt-2 space-y-3">
          {sections.map((s, i) => (
            <li key={i} className="rounded-lg border border-neutral-200 p-4">
              <div className="flex flex-wrap items-center gap-2">
                <p className="mr-1 font-serif text-neutral-900">
                  {i + 1}. {s.title}
                </p>
                {s.needs_evidence ? <WarnBadge /> : null}
              </div>
              {s.basis && s.basis.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {s.basis.map((cid) => (
                    <CitationBadge key={cid} id={cid} />
                  ))}
                </div>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function ValidateResultView({ data, onCopy }: { data: ValidateResponse; onCopy: (text: string, index: number) => void }) {
  const { t } = useTranslation();
  const strippedList = strippedItems(data.stripped);
  const report = data.verification;
  // 有报告就以三态口径展示（pending 不算通过）；旧服务端无报告时退回二态徽标
  const allPassed = report ? report.counts.blocked === 0 && report.counts.pending === 0 : Boolean(data.valid);
  return (
    <div className="mt-3 space-y-3">
      <span
        className={`inline-block rounded-full border px-3 py-0.5 text-sm ${
          allPassed ? "border-green-300 bg-green-50 text-green-800" : "border-red-300 bg-red-50 text-red-800"
        }`}
      >
        {allPassed ? t("workbench.writing.valid") : t("workbench.writing.invalid")}
      </span>
      {typeof data.stripped === "number" ? (
        <p className="text-sm text-neutral-500">{t("workbench.writing.strippedCount", { count: data.stripped })}</p>
      ) : strippedList.length ? (
        <div>
          <p className="text-sm text-neutral-500">{t("workbench.writing.stripped")}：</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-neutral-500">
            {strippedList.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {report ? <VerifyReportView data={data} /> : null}

      {data.cleaned_text ? (
        <div>
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-neutral-500">{t("workbench.writing.cleaned")}</p>
            <button
              type="button"
              onClick={() => onCopy(data.cleaned_text ?? "", -1)}
              className="rounded border border-neutral-300 px-2 py-0.5 text-xs text-neutral-500 hover:border-neutral-900 hover:text-neutral-900"
            >
              {t("workbench.writing.copy")}
            </button>
          </div>
          <pre className="mt-1 whitespace-pre-wrap rounded border border-neutral-100 bg-neutral-50 p-3 text-sm text-neutral-700">
            {data.cleaned_text}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

/** stripped 防御式规整：数组 → 各项文本；数字 → 单元素计数口径；字符串 → 单元素。 */
function strippedItems(stripped: unknown): string[] {
  if (Array.isArray(stripped)) return stripped.map((s) => (typeof s === "string" ? s : JSON.stringify(s)));
  if (typeof stripped === "number") return [String(stripped)];
  if (typeof stripped === "string" && stripped) return [stripped];
  return [];
}

// ---- 引用核验报告（task 1.6）：汇总 + 逐条定位 + 原文高亮跳转 ----

/** 高亮区间：只标「要人动手」的两态；pass 留在清单里，不刷屏。 */
interface Mark {
  start: number;
  end: number;
  status: VerifyStatus;
}

/** 报告条目 → 区间。同一引用可能同时缺上游与缺全文，区间会重叠，先归并。 */
function collectMarks(items: VerificationItem[]): Mark[] {
  const raw: Mark[] = [];
  for (const item of items) {
    const { char_start: s, char_end: e } = item.locator;
    if (item.status === "pass" || s == null || e == null || e <= s) continue;
    raw.push({ start: s, end: e, status: item.status });
  }
  raw.sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: Mark[] = [];
  for (const m of raw) {
    const last = merged[merged.length - 1];
    if (last && m.start <= last.end) {
      last.end = Math.max(last.end, m.end);
      // 拦截优先于待核：一处重叠时按更严的处置着色
      if (m.status === "block") last.status = "block";
    } else {
      merged.push({ ...m });
    }
  }
  return merged;
}

/** 按换行切段并保留原文偏移，供「跳转到对应段落」用。 */
function splitParagraphs(text: string): { start: number; end: number; text: string }[] {
  const out: { start: number; end: number; text: string }[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "\n") {
      out.push({ start, end: i, text: text.slice(start, i) });
      start = i + 1;
    }
  }
  out.push({ start, end: text.length, text: text.slice(start) });
  return out;
}

const MARK_CLASS: Record<string, string> = {
  block: "bg-red-100 text-red-900 underline decoration-red-400 decoration-2",
  pending_review: "bg-amber-100 text-amber-900 underline decoration-amber-400 decoration-2",
};

/** 一段原文 + 落在该段内的标记区间。 */
function Paragraph({
  index,
  text,
  start,
  marks,
}: {
  index: number;
  text: string;
  start: number;
  marks: Mark[];
}) {
  const inner: ReactNode[] = [];
  let cursor = 0;
  for (const m of marks) {
    const from = Math.max(m.start - start, 0);
    const to = Math.min(m.end - start, text.length);
    if (to <= cursor || from >= text.length) continue;
    if (from > cursor) inner.push(<span key={`t${cursor}`}>{text.slice(cursor, from)}</span>);
    inner.push(
      <mark key={`m${from}`} className={MARK_CLASS[m.status] ?? MARK_CLASS.pending_review}>
        {text.slice(Math.max(from, cursor), to)}
      </mark>,
    );
    cursor = to;
  }
  if (cursor < text.length) inner.push(<span key={`t${cursor}`}>{text.slice(cursor)}</span>);
  return (
    <p id={`verify-para-${index}`} className="min-h-[1.5rem] scroll-mt-24">
      {inner.length ? inner : text || "\u00a0"}
    </p>
  );
}

/** 核验报告：汇总三态 → 逐条（拦截/待核在前）→ 原文按 locator 高亮并可跳转。 */
function VerifyReportView({ data }: { data: ValidateResponse }) {
  const { t } = useTranslation();
  const [active, setActive] = useState<number | null>(null);
  const report = data.verification;
  if (!report) return null;

  const rank: Record<VerifyStatus, number> = { block: 0, pending_review: 1, pass: 2 };
  const items = [...report.items].sort((a, b) => rank[a.status] - rank[b.status]);
  const text = data.text ?? data.cleaned_text ?? "";
  const paragraphs = splitParagraphs(text);
  const marks = collectMarks(report.items);
  const mode = data.verification_mode ?? "shadow";

  const jump = (index: number) => {
    setActive(index);
    const start = items[index]?.locator.char_start;
    if (start == null) return;
    const p = paragraphs.findIndex((para) => start >= para.start && start <= para.end);
    if (p < 0) return;
    document.getElementById(`verify-para-${p}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  return (
    <div className="mt-3 space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="rounded-full border border-green-300 bg-green-50 px-3 py-0.5 text-green-800">
          {t("workbench.writing.verify.passCount", { count: report.counts.passed })}
        </span>
        <span className="rounded-full border border-red-300 bg-red-50 px-3 py-0.5 text-red-800">
          {t("workbench.writing.verify.blockCount", { count: report.counts.blocked })}
        </span>
        <span className="rounded-full border border-amber-300 bg-amber-50 px-3 py-0.5 text-amber-800">
          {t("workbench.writing.verify.pendingCount", { count: report.counts.pending })}
        </span>
        <span className="rounded-full border border-neutral-300 bg-neutral-50 px-3 py-0.5 text-neutral-600">
          {t(`workbench.writing.verify.mode.${mode}`)}
        </span>
        <span className="text-xs text-neutral-400">
          {t("workbench.writing.verify.reportVersion", { version: report.report_version })}
        </span>
      </div>

      {items.length === 0 ? (
        <EmptyLine>{t("workbench.writing.verify.empty")}</EmptyLine>
      ) : (
        <ol className="space-y-2">
          {items.map((item, i) => (
            <li key={`${item.rule_id}-${i}`}>
              <button
                type="button"
                onClick={() => jump(i)}
                disabled={item.locator.char_start == null}
                className={`w-full rounded border px-3 py-2 text-left transition-colors ${
                  active === i ? "border-neutral-900 bg-neutral-50" : "border-neutral-100 hover:border-neutral-400"
                } disabled:cursor-default`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <VerifyStatusBadge status={item.status} />
                  <span className="text-xs text-neutral-400">{item.level}</span>
                  <span className="font-mono text-xs text-neutral-400">{item.rule_id}</span>
                  {item.locator.citation_id ? (
                    <CitationBadge id={item.locator.citation_id} />
                  ) : null}
                  {item.locator.char_start != null ? (
                    <span className="ml-auto text-xs text-neutral-400">
                      {t("workbench.writing.verify.locate", {
                        start: item.locator.char_start + 1,
                        end: item.locator.char_end ?? item.locator.char_start + 1,
                      })}
                    </span>
                  ) : null}
                </div>
                <p className="mt-1 text-sm text-neutral-700">{item.reason}</p>
                {item.evidence.length ? (
                  <p className="mt-0.5 text-xs text-neutral-400">
                    {t("workbench.writing.verify.evidence")}：
                    {item.evidence
                      .map((e) => (e.detail ? `${e.source} · ${e.detail}` : e.source))
                      .join("；")}
                  </p>
                ) : null}
              </button>
            </li>
          ))}
        </ol>
      )}

      {text ? (
        <div>
          <p className="text-sm text-neutral-500">{t("workbench.writing.verify.located")}</p>
          <div className="mt-1 max-h-72 overflow-auto rounded border border-neutral-100 bg-neutral-50 p-3 font-serif text-sm leading-7 text-neutral-700">
            {paragraphs.map((para, i) => (
              <Paragraph
                key={i}
                index={i}
                text={para.text}
                start={para.start}
                marks={marks.filter((m) => m.start < para.end + 1 && m.end > para.start)}
              />
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

// ---- 容器：接 API ----

export default function WritingPanel({
  projects,
  currentProjectId,
  onSelectProject,
  onProjectsChanged,
}: {
  projects: Loadable<Project[]>;
  currentProjectId: string | null;
  onSelectProject: (id: string) => void;
  onProjectsChanged: () => void;
}) {
  const { t } = useTranslation();
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createNote, setCreateNote] = useState<string | null>(null);
  const [format, setFormat] = useState<CitationFormat>("gbt7714");
  const [citations, setCitations] = useState<Loadable<string[]>>({ status: "idle" });
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);
  const [outline, setOutline] = useState<Loadable<OutlineResponse>>({ status: "idle" });
  const [outlining, setOutlining] = useState(false);
  const [validateText, setValidateText] = useState("");
  const [validating, setValidating] = useState(false);
  const [validateResult, setValidateResult] = useState<Loadable<ValidateResponse>>({ status: "idle" });

  // 换项目：清提纲/校验，重载引用清单（含格式切换）
  useEffect(() => {
    setOutline({ status: "idle" });
    setValidateResult({ status: "idle" });
    if (!currentProjectId) {
      setCitations({ status: "idle" });
      return;
    }
    let alive = true;
    setCitations({ status: "loading" });
    void (async () => {
      try {
        const data = await api.listCitations(currentProjectId, format);
        if (alive) setCitations({ status: "ready", data: normalizeCitations(data) });
      } catch (e) {
        if (alive) setCitations(mapApiError(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [currentProjectId, format]);

  const create = async () => {
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    setCreateNote(null);
    try {
      const resp = await api.createProject(name);
      const fresh = normalizeProjects([resp])[0];
      setNewName("");
      setCreateNote(t("workbench.writing.created", { name }));
      onProjectsChanged();
      if (fresh) onSelectProject(fresh.id);
    } catch (e) {
      const m = mapApiError(e);
      setCreateNote(m.status === "error" ? m.message : t("workbench.unavailable"));
    } finally {
      setCreating(false);
    }
  };

  const genOutline = async () => {
    if (!currentProjectId) return;
    setOutlining(true);
    setOutline({ status: "loading" });
    try {
      setOutline({ status: "ready", data: await api.outline(currentProjectId) });
    } catch (e) {
      setOutline(mapApiError(e));
    } finally {
      setOutlining(false);
    }
  };

  const doValidate = async () => {
    if (!currentProjectId || !validateText.trim()) return;
    setValidating(true);
    setValidateResult({ status: "loading" });
    try {
      setValidateResult({ status: "ready", data: await api.validate(currentProjectId, validateText.trim()) });
    } catch (e) {
      setValidateResult(mapApiError(e));
    } finally {
      setValidating(false);
    }
  };

  const copy = async (text: string, index: number) => {
    setCopyFailed(false);
    try {
      await navigator.clipboard.writeText(text);
      setCopiedIndex(index);
      setTimeout(() => setCopiedIndex(null), 1500);
    } catch {
      setCopyFailed(true);
    }
  };

  return (
    <WritingView
      projects={projects}
      currentProjectId={currentProjectId}
      onSelectProject={onSelectProject}
      onReloadProjects={onProjectsChanged}
      newName={newName}
      onNewName={setNewName}
      onCreate={() => void create()}
      creating={creating}
      createNote={createNote}
      citations={citations}
      format={format}
      onFormat={setFormat}
      onCopy={(text, i) => void copy(text, i)}
      copiedIndex={copiedIndex}
      copyFailed={copyFailed}
      outline={outline}
      onOutline={() => void genOutline()}
      outlining={outlining}
      validateText={validateText}
      onValidateText={setValidateText}
      onValidate={() => void doValidate()}
      validating={validating}
      validateResult={validateResult}
    />
  );
}
