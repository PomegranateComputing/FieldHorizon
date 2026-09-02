import { useTranslation } from "react-i18next";

interface SectionPageProps {
  title: string;
}

/** Structural placeholder -- the real screen for each section is Phase UI-4's job. */
export function SectionPage({ title }: SectionPageProps) {
  const { t } = useTranslation();
  return (
    <div className="section-page">
      <h1>{title}</h1>
      <p className="type-label">{t("sectionPage.notBuilt")}</p>
    </div>
  );
}
