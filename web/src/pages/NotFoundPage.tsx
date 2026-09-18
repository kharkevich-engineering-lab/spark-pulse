/** The page for an address this application does not have.
 *
 * There was no such page. The backend serves `index.html` for any path it does
 * not recognise — that is how a single-page app has to work — and the router
 * then matched nothing and rendered an empty shell inside the sidebar. A typed
 * URL, a stale bookmark or a renamed route all produced a blank panel with no
 * explanation and no way back except the browser's own controls.
 */

import { Link } from "react-router-dom";
import { Home } from "lucide-react";
import { PulseIcon } from "@/components/BrandIcons";
import { useT } from "@/lib/i18n";
import { BrandFooter } from "@/components/BrandFooter";
import { PageHeader } from "@/ui";

export default function NotFoundPage() {
  const t = useT();
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <div className="flex-1 flex items-center justify-center px-6">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
            <PulseIcon className="text-blue2" size={32} />
          </div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.18em] text-brand-accent">404</p>
          <PageHeader
            title={t("notFound.heading")}
            className="mt-2 mb-0 min-[900px]:grid-cols-1 justify-items-center"
          />
          <p className="mt-3 max-w-md text-[15px] text-muted">{t("notFound.body")}</p>
          <Link
            to="/"
            className="mt-6 inline-flex items-center gap-2 px-[18px] py-[11px] rounded-sm bg-blue-cta hover:bg-primary-hover text-white font-semibold text-[14px] transition-colors"
          >
            <Home size={16} />
            {t("notFound.home")}
          </Link>
        </div>
      </div>
      <BrandFooter />
    </div>
  );
}
