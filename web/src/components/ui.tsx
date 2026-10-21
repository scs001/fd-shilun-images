/** 工作台共用小部件：状态徽标、降级/错误/加载盒、分节标题。无外部 UI 库。
 *  照抄 ../fd-zhesi/web/src/components/ui.tsx（去哲思专有的 Stability 徽标，换实论的引证徽标）。 */
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Loadable } from "../api/state";

export function Loading() {
  const { t } = useTranslation();
  return <p className="animate-pulse text-sm text-neutral-400">{t("workbench.loading")}</p>;
}

/** 「该能力接线中」降级盒（检索/公文/写作 5xx/超时/未接线）。 */
export function DegradeBox({ extra }: { extra?: ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
      <p>{t("workbench.unavailable")}</p>
      {extra ? <div className="mt-1 text-xs text-amber-700">{extra}</div> : null}
    </div>
  );
}

/** 业务错误盒（4xx 等）；401 追加「请先在顶栏设置访问令牌」。 */
export function ErrorBox({ message, code }: { message: string; code?: number }) {
  const { t } = useTranslation();
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
      <p>
        {t("workbench.error")}
        {message ? `：${message}` : ""}
      </p>
      {code === 401 ? <p className="mt-1 text-xs">{t("workbench.tokenHint")}</p> : null}
    </div>
  );
}

/** Loadable 四态统一渲染：idle 空、loading、unavailable 降级、error 报错、ready 渲染 children。 */
export function LoadBox<T>({
  state,
  children,
}: {
  state: Loadable<T>;
  children: (data: T) => ReactNode;
}) {
  if (state.status === "idle") return null;
  if (state.status === "loading") return <Loading />;
  if (state.status === "unavailable") return <DegradeBox />;
  if (state.status === "error") return <ErrorBox message={state.message} code={state.code} />;
  return <>{children(state.data)}</>;
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="font-serif text-lg text-neutral-900">{children}</h2>;
}

export function Note({ children }: { children: ReactNode }) {
  return <p className="text-sm text-green-700">{children}</p>;
}

export function EmptyLine({ children }: { children: ReactNode }) {
  return <p className="text-sm text-neutral-400">{children}</p>;
}

/** 依据引证徽标（提纲节 basis 的 citation_id）。 */
export function CitationBadge({ id }: { id: string }) {
  const { t } = useTranslation();
  return (
    <span className="shrink-0 rounded-full border border-neutral-300 bg-white px-2 py-0.5 text-xs text-neutral-600">
      {t("workbench.writing.basis", { id })}
    </span>
  );
}

/** 「需补证」黄标（提纲节 needs_evidence）。 */
export function WarnBadge() {
  const { t } = useTranslation();
  return (
    <span className="shrink-0 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-xs text-amber-800">
      {t("workbench.writing.needsEvidence")}
    </span>
  );
}

/** 核验状态徽标（pass / block / pending_review）。 */
export function VerifyStatusBadge({ status }: { status: "pass" | "block" | "pending_review" }) {
  const { t } = useTranslation();
  const styles: Record<string, string> = {
    pass: "border-green-300 bg-green-50 text-green-800",
    block: "border-red-300 bg-red-50 text-red-800",
    pending_review: "border-amber-300 bg-amber-50 text-amber-800",
  };
  return (
    <span className={`shrink-0 rounded-full border px-2 py-0.5 text-xs ${styles[status] ?? "border-neutral-300"}`}>
      {t(`workbench.writing.verify.status.${status}`)}
    </span>
  );
}
