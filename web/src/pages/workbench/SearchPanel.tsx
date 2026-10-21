/** 检索 · 论文查证：关键词/DOI 双模式 → 结果列表（出处/被引/来源徽标）→ 收藏到当前项目。 */
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, projectLabel, type Project, type SearchResponse, type WorkResult } from "../../api/client";
import { Loadable, mapApiError } from "../../api/state";
import { DegradeBox, EmptyLine, ErrorBox, Loading, Note, SectionTitle } from "../../components/ui";

export type SearchMode = "query" | "doi";

// ---- 视图（显式状态，可独立渲染降级态） ----

export function SearchView({
  mode,
  onMode,
  text,
  onText,
  onSubmit,
  busy,
  result,
  currentProjectLabel,
  favoritedIds,
  favoriteBusyId,
  favoriteNote,
  onFavorite,
  refine,
  onRefine,
}: {
  mode: SearchMode;
  onMode: (m: SearchMode) => void;
  text: string;
  onText: (v: string) => void;
  onSubmit: () => void;
  busy: boolean;
  result: Loadable<SearchResponse>;
  currentProjectLabel: string | null;
  favoritedIds: string[];
  favoriteBusyId: string | null;
  favoriteNote: string | null;
  onFavorite: (work: WorkResult) => void;
  // 可选：传了才渲染勾选框（只读渲染场景不必关心精排）
  refine?: boolean;
  onRefine?: (v: boolean) => void;
}) {
  const { t } = useTranslation();
  const canFavorite = Boolean(currentProjectLabel);
  return (
    <section className="space-y-6">
      <div>
        <SectionTitle>{t("workbench.search.heading")}</SectionTitle>
        <p className="mt-1 text-sm text-neutral-500">{t("workbench.search.hint")}</p>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <div className="flex overflow-hidden rounded border border-neutral-300 text-xs" role="group" aria-label={t("workbench.search.modeLabel")}>
            {(["query", "doi"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => onMode(m)}
                className={`px-3 py-1.5 ${
                  mode === m ? "bg-neutral-900 text-white" : "bg-white text-neutral-500 hover:bg-neutral-100"
                }`}
              >
                {t(`workbench.search.mode.${m}`)}
              </button>
            ))}
          </div>
          <input
            value={text}
            onChange={(e) => onText(e.target.value)}
            placeholder={t(`workbench.search.placeholder.${mode}`)}
            className="min-w-0 flex-1 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
          />
          <button
            type="button"
            disabled={busy || !text.trim()}
            onClick={onSubmit}
            className="rounded-lg bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? t("workbench.search.searching") : t("workbench.search.submit")}
          </button>
          {onRefine ? (
            <label className="flex items-center gap-1.5 text-xs text-neutral-600">
              <input
                type="checkbox"
                checked={Boolean(refine)}
                onChange={(e) => onRefine(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-neutral-300"
              />
              {t("workbench.search.refine")}
            </label>
          ) : null}
        </div>
        {!canFavorite ? (
          <p className="mt-2 text-xs text-neutral-400">{t("workbench.project.hint")}</p>
        ) : null}
      </div>

      {result.status === "loading" ? <Loading /> : null}
      {result.status === "unavailable" ? <DegradeBox /> : null}
      {result.status === "error" ? <ErrorBox message={result.message} code={result.code} /> : null}
      {result.status === "ready" ? <SearchResultView data={result.data} currentProjectLabel={currentProjectLabel} favoritedIds={favoritedIds} favoriteBusyId={favoriteBusyId} onFavorite={onFavorite} /> : null}

      {favoriteNote ? <Note>{favoriteNote}</Note> : null}
    </section>
  );
}

function SearchResultView({
  data,
  currentProjectLabel,
  favoritedIds,
  favoriteBusyId,
  onFavorite,
}: {
  data: SearchResponse;
  currentProjectLabel: string | null;
  favoritedIds: string[];
  favoriteBusyId: string | null;
  onFavorite: (work: WorkResult) => void;
}) {
  const { t } = useTranslation();
  return (
    <div>
      {/* 覆盖不完整必须显式说出来，别让用户把部分覆盖当成查全 */}
      {data.coverage_note ? (
        <p
          role="note"
          className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900"
        >
          {data.coverage_note}
        </p>
      ) : null}
      {data.results.length === 0 ? (
        <div className="space-y-2">
          <EmptyLine>{t("workbench.search.resultsEmpty")}</EmptyLine>
          {data.reason ? (
            <p className="text-sm text-neutral-500">{t("workbench.search.reason", { reason: data.reason })}</p>
          ) : null}
          {data.detail ? <p className="text-xs text-neutral-400">{data.detail}</p> : null}
        </div>
      ) : (
        <ul className="space-y-3">
          {data.results.map((w) => {
            const favorited = favoritedIds.includes(w.work_id);
            return (
              <li key={w.work_id} className="rounded-lg border border-neutral-200 bg-white p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="font-serif text-neutral-900">{w.title}</h3>
                    <p className="mt-1 text-xs text-neutral-500">
                      {workMeta(w)}
                    </p>
                    {/* 部分覆盖摊开：让用户看出这条只讲到查询的哪一部分 */}
                    {w.provenance?.matched_terms?.length ? (
                      <p className="mt-1 text-xs text-amber-700">
                        {t("workbench.search.coveredTerms", {
                          terms: w.provenance.matched_terms.join("、"),
                        })}
                      </p>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    disabled={!currentProjectLabel || favorited || favoriteBusyId === w.work_id}
                    onClick={() => onFavorite(w)}
                    className={`shrink-0 rounded border px-3 py-1 text-xs transition-colors disabled:cursor-not-allowed ${
                      favorited
                        ? "border-green-300 bg-green-50 text-green-700"
                        : "border-neutral-900 text-neutral-900 hover:bg-neutral-900 hover:text-white disabled:opacity-40"
                    }`}
                    title={currentProjectLabel ?? t("workbench.project.hint")}
                  >
                    {favorited
                      ? t("workbench.search.favorited")
                      : favoriteBusyId === w.work_id
                        ? t("workbench.loading")
                        : t("workbench.search.favorite")}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function workMeta(w: WorkResult): string {
  const parts: string[] = [];
  if (w.year != null) parts.push(String(w.year));
  if (w.venue) parts.push(w.venue);
  if (w.cited_by_count != null) parts.push(`${w.cited_by_count}`);
  if (w.provenance?.source) parts.push(w.provenance.source);
  if (w.provenance?.authorization?.channel) parts.push(w.provenance.authorization.channel);
  return parts.join(" · ");
}

// ---- 容器：接 API ----

export default function SearchPanel({ currentProject }: { currentProject: Project | null }) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<SearchMode>("query");
  const [text, setText] = useState("");
  const [refine, setRefine] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Loadable<SearchResponse>>({ status: "idle" });
  const [favoritedIds, setFavoritedIds] = useState<string[]>([]);
  const [favoriteBusyId, setFavoriteBusyId] = useState<string | null>(null);
  const [favoriteNote, setFavoriteNote] = useState<string | null>(null);

  const submit = async () => {
    const q = text.trim();
    if (!q) return;
    setBusy(true);
    setResult({ status: "loading" });
    try {
      const data =
        mode === "doi"
          ? await api.searchWorks({ doi: q })
          : await api.searchWorks({ query: q, ...(refine ? { rerank: true } : {}) });
      setResult({ status: "ready", data });
    } catch (e) {
      setResult(mapApiError(e));
    } finally {
      setBusy(false);
    }
  };

  const favorite = async (work: WorkResult) => {
    if (!currentProject) return;
    setFavoriteBusyId(work.work_id);
    setFavoriteNote(null);
    try {
      await api.addCitation(currentProject.id, work);
      setFavoritedIds((ids) => (ids.includes(work.work_id) ? ids : [...ids, work.work_id]));
      setFavoriteNote(t("workbench.search.favoriteOk", { project: projectLabel(currentProject) }));
    } catch (e) {
      const m = mapApiError(e);
      setFavoriteNote(m.status === "error" ? m.message : t("workbench.unavailable"));
    } finally {
      setFavoriteBusyId(null);
    }
  };

  return (
    <SearchView
      mode={mode}
      onMode={setMode}
      text={text}
      onText={setText}
      onSubmit={() => void submit()}
      busy={busy}
      result={result}
      currentProjectLabel={currentProject ? projectLabel(currentProject) : null}
      favoritedIds={favoritedIds}
      favoriteBusyId={favoriteBusyId}
      favoriteNote={favoriteNote}
      onFavorite={(w) => void favorite(w)}
      refine={refine}
      onRefine={setRefine}
    />
  );
}
