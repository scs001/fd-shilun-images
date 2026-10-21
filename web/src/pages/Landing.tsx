import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

/** 引用条目显影——页面唯一的动效时刻：三条真实格式的引文逐行显影。 */
function CitationCard() {
  const { t } = useTranslation();
  const lines = [
    { cls: "cite-line cite-line-1", body: t("landing.visual.cite1") },
    { cls: "cite-line cite-line-2", body: t("landing.visual.cite2") },
    { cls: "cite-line cite-line-3", body: t("landing.visual.cite3") },
  ];
  return (
    <figure className="rounded-xl border border-line bg-white p-6 shadow-[0_1px_2px_rgba(28,28,30,0.04),0_8px_28px_rgba(28,28,30,0.07)]">
      <figcaption className="text-xs font-medium tracking-wide text-scholar">{t("landing.visual.caption")}</figcaption>
      <ol className="mt-4 space-y-3.5">
        {lines.map((l) => (
          <li key={l.cls} className={`${l.cls} font-display text-[13px] leading-relaxed text-ink/90 md:text-sm`}>
            {l.body}
          </li>
        ))}
      </ol>
      <p className="cite-line cite-line-3 mt-4 border-t border-line pt-3 text-xs text-ink-soft">
        {t("landing.visual.note")}
      </p>
    </figure>
  );
}

function Band({ index, name, title, body, detail }: { index: number; name: string; title: string; body: string; detail: string }) {
  return (
    <section className={`border-t border-line ${index % 2 === 1 ? "bg-mist" : "bg-paper"}`}>
      <div className="mx-auto grid max-w-5xl gap-6 px-6 py-14 md:grid-cols-[10rem_1fr] md:gap-12 md:py-16">
        <div className="font-display text-2xl text-scholar md:text-right">{name}</div>
        <div className="max-w-2xl">
          <h2 className="font-display text-2xl leading-snug text-ink md:text-[1.7rem]">{title}</h2>
          <p className="mt-4 leading-loose text-ink-soft">{body}</p>
          <p className="mt-3 text-sm leading-relaxed text-ink-soft/90">{detail}</p>
        </div>
      </div>
    </section>
  );
}

export default function Landing() {
  const { t } = useTranslation();
  const bands = (["search", "gov", "writing"] as const).map((k) => ({
    name: t(`landing.loops.${k}.name`),
    title: t(`landing.loops.${k}.title`),
    body: t(`landing.loops.${k}.body`),
    detail: t(`landing.loops.${k}.detail`),
  }));
  return (
    <main className="bg-paper">
      {/* Hero——白纸黑字 */}
      <section className="bg-paper">
        <div className="mx-auto grid max-w-6xl items-center gap-12 px-6 py-20 lg:grid-cols-12 lg:py-28">
          <div className="lg:col-span-7">
            <h1 className="hero-rise font-display text-5xl leading-tight tracking-wide text-ink md:text-6xl">
              {t("landing.hero.statement1")}
            </h1>
            <p className="hero-rise hero-rise-1 mt-5 font-display text-2xl leading-snug text-ink/80 md:text-3xl">
              {t("landing.hero.statement2")}
            </p>
            <p className="hero-rise hero-rise-2 mt-7 max-w-xl leading-loose text-ink-soft">
              {t("landing.hero.lede")}
            </p>
            <div className="hero-rise hero-rise-3 mt-10 flex flex-wrap items-center gap-4">
              <Link to="/workbench" className="rounded-md bg-ink px-6 py-3 text-sm font-medium text-paper transition-colors hover:bg-black">
                {t("landing.hero.cta")}
              </Link>
              <a href="#loops" className="rounded-md border border-line-strong px-6 py-3 text-sm text-ink transition-colors hover:border-ink">
                {t("landing.hero.ctaSecondary")}
              </a>
            </div>
          </div>
          <div className="hero-rise hero-rise-2 lg:col-span-5">
            <CitationCard />
          </div>
        </div>
      </section>

      <div id="loops" className="scroll-mt-16">
        {bands.map((b, i) => (
          <Band key={b.name} index={i} {...b} />
        ))}
      </div>

      {/* 引用可解——产品真话 */}
      <section className="border-t border-line bg-night-band text-moon">
        <div className="mx-auto max-w-3xl px-6 py-16 text-center">
          <h2 className="font-display text-2xl leading-snug text-white md:text-3xl">{t("landing.truth.title")}</h2>
          <p className="mx-auto mt-5 max-w-xl leading-loose text-moon/70">{t("landing.truth.body")}</p>
        </div>
      </section>

      {/* CTA */}
      <section className="border-t border-line bg-paper">
        <div className="mx-auto max-w-3xl px-6 py-20 text-center">
          <h2 className="font-display text-3xl leading-snug text-ink">{t("landing.cta.title")}</h2>
          <p className="mt-4 leading-loose text-ink-soft">{t("landing.cta.body")}</p>
          <Link to="/workbench" className="mt-8 inline-block rounded-md bg-ink px-8 py-3.5 text-sm font-medium text-paper transition-colors hover:bg-black">
            {t("landing.hero.cta")}
          </Link>
          <p className="mt-6 text-xs text-ink-soft/70">{t("landing.cta.family")}</p>
        </div>
      </section>
    </main>
  );
}
