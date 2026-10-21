import { useTranslation } from "react-i18next";

export default function Admin() {
  const { t } = useTranslation();
  return (
    <main className="mx-auto max-w-3xl px-6 py-20">
      <h1 className="text-2xl font-serif">{t("admin.title")}</h1>
      <p className="mt-4 text-neutral-500">{t("admin.placeholder")}</p>
    </main>
  );
}
