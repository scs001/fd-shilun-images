// Logto OIDC 登录流（authorization code + PKCE，原生 fetch/crypto，零依赖）。
// 照抄 ../fd-zhesi/web/src/api/logto.ts；token 写 localStorage["shilun_token"]，
// 与 api/client.ts 的读取约定一致（client.setToken 同键）。

import { setToken } from "./client";

const ISSUER = "https://auth.finddatatech.cloud/oidc";
const CLIENT_ID = "aqqinerulglscp6tw1fy0"; // Logto: shilun-workbench（SPA）
const REDIRECT_URI = `${window.location.origin}/workbench`;
const SCOPE = "openid profile offline_access";

const TOKEN_KEY = "shilun_token";
const VERIFIER_KEY = "shilun_pkce_verifier";
const STATE_KEY = "shilun_oidc_state";

export function currentToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function isLoggedIn(): boolean {
  return Boolean(currentToken());
}

function randomString(len = 48): string {
  const bytes = new Uint8Array(len);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(36).padStart(2, "0")).join("").slice(0, 64);
}

async function sha256B64Url(input: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

export async function login(): Promise<void> {
  const verifier = randomString();
  const state = randomString(16);
  localStorage.setItem(VERIFIER_KEY, verifier);
  localStorage.setItem(STATE_KEY, state);
  const challenge = await sha256B64Url(verifier);
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    response_type: "code",
    scope: SCOPE,
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  window.location.assign(`${ISSUER}/auth?${params}`);
}

/** /workbench 挂载时调用：带 code 回跳则换 token，成功后清 URL 参数。 */
export async function handleCallback(): Promise<boolean> {
  const url = new URL(window.location.href);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  if (!code) return false;
  const expected = localStorage.getItem(STATE_KEY);
  if (!state || !expected || state !== expected) {
    url.searchParams.delete("code");
    url.searchParams.delete("state");
    window.history.replaceState({}, "", url.pathname);
    return false;
  }
  const verifier = localStorage.getItem(VERIFIER_KEY) ?? "";
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: REDIRECT_URI,
    client_id: CLIENT_ID,
    code_verifier: verifier,
  });
  const resp = await fetch(`${ISSUER}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  url.searchParams.delete("code");
  url.searchParams.delete("state");
  window.history.replaceState({}, "", url.pathname);
  localStorage.removeItem(STATE_KEY);
  localStorage.removeItem(VERIFIER_KEY);
  if (!resp.ok) return false;
  const token = await resp.json();
  if (token.id_token) {
    setToken(String(token.id_token));
    return true;
  }
  return false;
}

export function logout(): void {
  localStorage.removeItem(TOKEN_KEY);
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    post_logout_redirect_uri: `${window.location.origin}/`,
  });
  window.location.assign(`${ISSUER}/session/end?${params}`);
}
