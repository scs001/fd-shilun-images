import { useEffect, useState } from "react";
import { Link, Route, Routes } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { getToken, setToken } from "./api/client";
import { handleCallback, login, logout } from "./api/logto";
import Landing from "./pages/Landing";
import Workbench from "./pages/Workbench";
import Admin from "./pages/Admin";

export default function App() {
  const { t, i18n } = useTranslation();
  const [token, setTokenState] = useState(() => getToken());
  const [lang, setLang] = useState(i18n.language ?? "zh");

  // /workbench 带 code 回跳时静默换 token（PKCE），成功后刷新本地态
  useEffect(() => {
    void handleCallback().then((ok) => {
      if (ok) setTokenState(getToken() ?? "");
    });
  }, []);

  const changeLang = (l: string) => {
    setLang(l);
    void i18n.changeLanguage(l);
  };

  return (
    <div className="min-h-screen bg-neutral-50 text-neutral-900">
      <nav className="flex items-center gap-6 border-b border-neutral-200 px-6 py-3 text-sm">
        <Link to="/">{t("nav.home")}</Link>
        <Link to="/workbench">{t("nav.workbench")}</Link>
        <Link to="/admin">{t("nav.admin")}</Link>

        <div className="ml-auto flex items-center gap-3">
          {token ? (
            <button
              type="button"
              onClick={() => logout()}
              className="rounded border border-neutral-300 bg-white px-2 py-1 text-xs text-neutral-600 hover:bg-neutral-100"
            >
              {t("nav.logout")}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void login()}
              className="rounded bg-neutral-900 px-2 py-1 text-xs text-white hover:bg-neutral-700"
            >
              {t("nav.login")}
            </button>
          )}
          {/* 开发态兜底：手动填访问令牌（存 localStorage["shilun_token"]） */}
          <input
            aria-label={t("nav.token")}
            title={t("nav.token")}
            value={token}
            placeholder={t("nav.token")}
            autoComplete="off"
            onChange={(e) => {
              setTokenState(e.target.value);
              setToken(e.target.value);
            }}
            className="w-40 rounded border border-neutral-300 bg-white px-2 py-1 text-xs text-neutral-700 focus:border-neutral-900 focus:outline-none"
          />
          <div className="flex overflow-hidden rounded border border-neutral-300 text-xs">
            {["zh", "en"].map((l) => (
              <button
                key={l}
                type="button"
                onClick={() => changeLang(l)}
                className={`px-2 py-1 ${
                  lang === l ? "bg-neutral-900 text-white" : "bg-white text-neutral-500 hover:bg-neutral-100"
                }`}
              >
                {l}
              </button>
            ))}
          </div>
        </div>
      </nav>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/workbench" element={<Workbench />} />
        <Route path="/admin" element={<Admin />} />
      </Routes>
    </div>
  );
}
