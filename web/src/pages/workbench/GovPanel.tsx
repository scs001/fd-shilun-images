/** 公文 · 骨架起草：文种下拉（doctypes）→ 模板预览（节+约束+样例计数）→ 主题+要点 → 骨架稿（样例可溯）。 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  api,
  itemText,
  textList,
  type DoctypeInfo,
  type GovDraftResponse,
  type GovTemplate,
} from "../../api/client";
import { Loadable, mapApiError } from "../../api/state";
import { DegradeBox, EmptyLine, ErrorBox, Loading, SectionTitle } from "../../components/ui";

// ---- 骨架稿防御式渲染：{sections:[...]} / 字符串 / 其它对象 ----

interface DraftEntry {
  title: string;
  body?: string;
}

function draftEntries(draft: unknown): DraftEntry[] | null {
  const arr = Array.isArray(draft)
    ? draft
    : draft && typeof draft === "object" && Array.isArray((draft as Record<string, unknown>).sections)
      ? (draft as Record<string, unknown>).sections
      : null;
  if (!Array.isArray(arr)) return null;
  return arr.map((item) => {
    if (typeof item === "string") return { title: item };
    if (item && typeof item === "object") {
      const o = item as Record<string, unknown>;
      const body = [o.content, o.body, o.text].find((v) => typeof v === "string" && v);
      return { title: itemText(o.title ?? item), body: body ? String(body) : undefined };
    }
    return { title: String(item) };
  });
}

function DraftView({ draft }: { draft: unknown }) {
  const { t } = useTranslation();
  const entries = draftEntries(draft);
  if (entries) {
    return (
      <ol className="mt-3 space-y-3">
        {entries.map((e, i) => (
          <li key={i} className="rounded-lg border border-neutral-200 bg-white p-4">
            <p className="font-serif text-neutral-900">
              {i + 1}. {e.title}
            </p>
            {e.body ? <p className="mt-1 whitespace-pre-wrap text-sm text-neutral-600">{e.body}</p> : null}
          </li>
        ))}
      </ol>
    );
  }
  if (typeof draft === "string") {
    return (
      <pre className="mt-3 whitespace-pre-wrap rounded-lg border border-neutral-200 bg-white p-4 text-sm text-neutral-700">
        {draft}
      </pre>
    );
  }
  return (
    <pre className="mt-3 overflow-x-auto rounded-lg border border-neutral-200 bg-white p-4 text-xs text-neutral-500">
      {safeJson(draft) ?? t("workbench.gov.draftEmpty")}
    </pre>
  );
}

function safeJson(v: unknown): string | null {
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return null;
  }
}

// ---- 视图（显式状态，可独立渲染降级态） ----

export function GovView({
  doctypes,
  doctype,
  onDoctype,
  onReloadDoctypes,
  template,
  subject,
  onSubject,
  keyPoints,
  onKeyPoints,
  onDraft,
  draft,
  busy,
}: {
  doctypes: Loadable<DoctypeInfo[]>;
  doctype: string;
  onDoctype: (d: string) => void;
  onReloadDoctypes: () => void;
  template: Loadable<GovTemplate>;
  subject: string;
  onSubject: (v: string) => void;
  keyPoints: string;
  onKeyPoints: (v: string) => void;
  onDraft: () => void;
  draft: Loadable<GovDraftResponse>;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const canDraft = Boolean(doctype) && subject.trim().length > 0;
  return (
    <section className="space-y-6">
      <div>
        <SectionTitle>{t("workbench.gov.heading")}</SectionTitle>
        <p className="mt-1 text-sm text-neutral-500">{t("workbench.gov.hint")}</p>
      </div>

      {/* 文种选择 */}
      <div>
        <div className="flex items-baseline justify-between gap-3">
          <label htmlFor="gov-doctype" className="text-sm text-neutral-600">
            {t("workbench.gov.doctypeLabel")}
          </label>
          <button
            type="button"
            onClick={onReloadDoctypes}
            className="text-xs text-neutral-500 underline hover:text-neutral-900"
          >
            {t("workbench.retry")}
          </button>
        </div>
        {doctypes.status === "loading" ? <Loading /> : null}
        {doctypes.status === "unavailable" ? (
          <div className="mt-2">
            <DegradeBox />
          </div>
        ) : null}
        {doctypes.status === "error" ? (
          <div className="mt-2">
            <ErrorBox message={doctypes.message} code={doctypes.code} />
          </div>
        ) : null}
        {doctypes.status === "ready" ? (
          doctypes.data.length === 0 ? (
            <EmptyLine>{t("workbench.gov.doctypesEmpty")}</EmptyLine>
          ) : (
            <select
              id="gov-doctype"
              value={doctype}
              onChange={(e) => onDoctype(e.target.value)}
              className="mt-2 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
            >
              <option value="">{t("workbench.gov.doctypePlaceholder")}</option>
              {doctypes.data.map((d) => (
                <option key={d.doctype} value={d.doctype}>
                  {d.doctype}
                  {typeof d.count === "number" ? `（${t("workbench.gov.doctypeCount", { count: d.count })}）` : ""}
                </option>
              ))}
            </select>
          )
        ) : null}
      </div>

      {/* 模板预览 */}
      {doctype ? (
        <div className="rounded-lg border border-neutral-200 bg-white p-5">
          <SectionTitle>{t("workbench.gov.template")}</SectionTitle>
          {template.status === "loading" ? <Loading /> : null}
          {template.status === "unavailable" ? (
            <div className="mt-2">
              <DegradeBox />
            </div>
          ) : null}
          {template.status === "error" ? (
            <div className="mt-2">
              <ErrorBox message={template.message} code={template.code} />
            </div>
          ) : null}
          {template.status === "ready" ? <TemplateView data={template.data} /> : null}
        </div>
      ) : null}

      {/* 主题 + 要点 → 骨架稿 */}
      <div className="rounded-lg border border-neutral-200 bg-white p-5">
        <SectionTitle>{t("workbench.gov.draft")}</SectionTitle>
        <input
          value={subject}
          onChange={(e) => onSubject(e.target.value)}
          placeholder={t("workbench.gov.subjectPlaceholder")}
          aria-label={t("workbench.gov.subjectLabel")}
          className="mt-3 w-full rounded border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
        />
        <textarea
          value={keyPoints}
          onChange={(e) => onKeyPoints(e.target.value)}
          placeholder={t("workbench.gov.keyPointsPlaceholder")}
          aria-label={t("workbench.gov.keyPointsLabel")}
          rows={4}
          className="mt-3 w-full rounded border border-neutral-300 px-3 py-2 text-sm focus:border-neutral-900 focus:outline-none"
        />
        <button
          type="button"
          disabled={!canDraft || busy}
          onClick={onDraft}
          className="mt-3 rounded-lg bg-neutral-900 px-4 py-2 text-sm text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? t("workbench.gov.drafting") : t("workbench.gov.draft")}
        </button>

        {draft.status === "loading" ? <Loading /> : null}
        {draft.status === "unavailable" ? (
          <div className="mt-3">
            <DegradeBox />
          </div>
        ) : null}
        {draft.status === "error" ? (
          <div className="mt-3">
            <ErrorBox message={draft.message} code={draft.code} />
          </div>
        ) : null}
        {draft.status === "ready" ? (
          <div className="mt-3">
            <DraftResultView data={draft.data} />
          </div>
        ) : null}
      </div>
    </section>
  );
}

function TemplateView({ data }: { data: GovTemplate }) {
  const { t } = useTranslation();
  const sections = textList(data.sections);
  const constraints = textList(data.constraints);
  const samples = Array.isArray(data.samples) ? data.samples : [];
  return (
    <div className="mt-3 space-y-4">
      {data.sample_insufficient ? (
        <p className="text-xs text-amber-700">{t("workbench.gov.sampleInsufficient")}</p>
      ) : null}

      <div>
        <h3 className="text-sm font-medium text-neutral-500">{t("workbench.gov.sections")}</h3>
        {sections.length ? (
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-neutral-700">
            {sections.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        ) : (
          <EmptyLine>{t("workbench.gov.samplesEmpty")}</EmptyLine>
        )}
      </div>

      {constraints.length ? (
        <div>
          <h3 className="text-sm font-medium text-neutral-500">{t("workbench.gov.constraints")}</h3>
          <ul className="mt-2 space-y-1 text-sm text-neutral-600">
            {constraints.map((c, i) => (
              <li key={i}>· {c}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div>
        <h3 className="text-sm font-medium text-neutral-500">
          {t("workbench.gov.samples", { count: samples.length })}
        </h3>
        {samples.length ? (
          <ul className="mt-2 space-y-1 text-xs text-neutral-500">
            {samples.map((s, i) => (
              <li key={i}>{itemText(s)}</li>
            ))}
          </ul>
        ) : (
          <EmptyLine>{t("workbench.gov.samplesEmpty")}</EmptyLine>
        )}
      </div>
    </div>
  );
}

function DraftResultView({ data }: { data: GovDraftResponse }) {
  const { t } = useTranslation();
  const sampleIds = Array.isArray(data.sample_ids) ? data.sample_ids : [];
  return (
    <div>
      {data.template_ref != null && data.template_ref !== "" ? (
        <p className="text-xs text-neutral-400">
          {t("workbench.gov.templateRef", { ref: itemText(data.template_ref) })}
        </p>
      ) : null}
      {sampleIds.length ? (
        <p className="mt-1 text-xs text-neutral-400">
          {t("workbench.gov.sampleTrace", { ids: sampleIds.join("、") })}
        </p>
      ) : null}
      <DraftView draft={data.draft} />
    </div>
  );
}

// ---- 容器：接 API ----

export default function GovPanel() {
  const [doctypes, setDoctypes] = useState<Loadable<DoctypeInfo[]>>({ status: "loading" });
  const [doctype, setDoctype] = useState("");
  const [template, setTemplate] = useState<Loadable<GovTemplate>>({ status: "idle" });
  const [subject, setSubject] = useState("");
  const [keyPoints, setKeyPoints] = useState("");
  const [draft, setDraft] = useState<Loadable<GovDraftResponse>>({ status: "idle" });
  const [busy, setBusy] = useState(false);

  const loadDoctypes = async () => {
    setDoctypes({ status: "loading" });
    try {
      const data = await api.listDoctypes();
      setDoctypes({ status: "ready", data: Array.isArray(data) ? data : [] });
    } catch (e) {
      setDoctypes(mapApiError(e));
    }
  };

  useEffect(() => {
    void loadDoctypes();
  }, []);

  const selectDoctype = (d: string) => {
    setDoctype(d);
    setDraft({ status: "idle" });
    setTemplate({ status: "idle" });
    if (!d) return;
    void (async () => {
      setTemplate({ status: "loading" });
      try {
        setTemplate({ status: "ready", data: await api.govTemplate(d) });
      } catch (e) {
        setTemplate(mapApiError(e));
      }
    })();
  };

  const genDraft = async () => {
    if (!doctype || !subject.trim()) return;
    setBusy(true);
    setDraft({ status: "loading" });
    try {
      setDraft({
        status: "ready",
        data: await api.govDraft({
          doctype,
          subject: subject.trim(),
          key_points: keyPoints
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean),
        }),
      });
    } catch (e) {
      setDraft(mapApiError(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <GovView
      doctypes={doctypes}
      doctype={doctype}
      onDoctype={selectDoctype}
      onReloadDoctypes={() => void loadDoctypes()}
      template={template}
      subject={subject}
      onSubject={setSubject}
      keyPoints={keyPoints}
      onKeyPoints={setKeyPoints}
      onDraft={() => void genDraft()}
      draft={draft}
      busy={busy}
    />
  );
}
