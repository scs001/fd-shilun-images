/**
 * 面向 UI 的加载态与错误映射（照抄 ../fd-zhesi/web/src/api/state.ts）。
 * fetch 挂掉/超时/5xx/接线期 404 → unavailable（「该能力接线中」）；4xx → error（业务报错）。
 */
import { ApiError, ApiUnavailableError } from "./client";

export type Loadable<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "unavailable" }
  | { status: "error"; message: string; code?: number };

export type Errored = Extract<Loadable<unknown>, { status: "error" }>;

export function mapApiError(e: unknown): Errored | { status: "unavailable" } {
  if (e instanceof ApiUnavailableError) return { status: "unavailable" };
  if (e instanceof ApiError) return { status: "error", message: e.message, code: e.status };
  return { status: "error", message: e instanceof Error ? e.message : String(e) };
}
