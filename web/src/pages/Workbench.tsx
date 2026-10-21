/** 工作台：检索 / 公文 / 写作 三 tab；项目态提升至此（检索收藏与写作页共享「当前项目」）。 */
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, normalizeProjects, projectLabel, type Project } from "../api/client";
import { Loadable, mapApiError } from "../api/state";
import SearchPanel from "./workbench/SearchPanel";
import GovPanel from "./workbench/GovPanel";
import WritingPanel from "./workbench/WritingPanel";

type Tab = "search" | "gov" | "writing";
const TABS: readonly Tab[] = ["search", "gov", "writing"];

export default function Workbench() {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("search");
  const [projects, setProjects] = useState<Loadable<Project[]>>({ status: "loading" });
  const [currentProjectId, setCurrentProjectId] = useState<string | null>(null);

  const loadProjects = useCallback(async () => {
    setProjects({ status: "loading" });
    try {
      setProjects({ status: "ready", data: normalizeProjects(await api.listProjects()) });
    } catch (e) {
      setProjects(mapApiError(e));
    }
  }, []);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  const currentProject =
    projects.status === "ready" ? projects.data.find((p) => p.id === currentProjectId) ?? null : null;

  return (
    <main className="mx-auto max-w-3xl px-6 py-10">
      <h1 className="text-2xl font-serif">{t("workbench.title")}</h1>
      <p className="mt-1 text-sm text-neutral-500">{t("workbench.subtitle")}</p>

      {/* 当前项目指示（检索收藏的目标，随写作页选择联动） */}
      <p className="mt-2 text-xs text-neutral-400">
        {t("workbench.project.label")}：
        {currentProject ? projectLabel(currentProject) : t("workbench.project.none")}
      </p>

      <div className="mt-6 flex gap-1 border-b border-neutral-200" role="tablist">
        {TABS.map((k) => (
          <button
            key={k}
            type="button"
            role="tab"
            aria-selected={tab === k}
            onClick={() => setTab(k)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm transition-colors ${
              tab === k
                ? "border-neutral-900 font-medium text-neutral-900"
                : "border-transparent text-neutral-500 hover:text-neutral-900"
            }`}
          >
            {t(`workbench.tabs.${k}`)}
          </button>
        ))}
      </div>

      <div className="mt-8">
        {tab === "search" ? <SearchPanel currentProject={currentProject} /> : null}
        {tab === "gov" ? <GovPanel /> : null}
        {tab === "writing" ? (
          <WritingPanel
            projects={projects}
            currentProjectId={currentProjectId}
            onSelectProject={setCurrentProjectId}
            onProjectsChanged={() => void loadProjects()}
          />
        ) : null}
      </div>
    </main>
  );
}
