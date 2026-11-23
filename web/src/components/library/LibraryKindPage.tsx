/** The one shape every Library page has: a catalogue, and where it comes from.
 *
 * Engines, models, recipes and mods each come from somewhere — an engine
 * index, a model hub, an OCI registry, a directory — and the place that said
 * where used to be somewhere else: Settings → Library for three of them, a
 * Collections page that listed the registries a second time, nothing at all
 * for mods. So each kind's page now has two sections, the same two on every
 * page: **Catalogue** (what the sources offer and what the nodes hold, with
 * one action per row) and **Sources** (where this kind comes from). The
 * section is the URL hash — `/recipes#sources` is a link somebody can send —
 * and the header's one primary action follows it: *Get* on the catalogue,
 * *Add source* on the sources.
 */

import type { ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { Button, PageHeader, Tabs } from "@/ui";

export type LibrarySection = "catalogue" | "sources";

export interface LibraryAction {
  label: string;
  icon?: LucideIcon;
  onClick: () => void;
}

export interface LibraryKindPageProps {
  title: string;
  /** One line under the title: what is on disk, how many are held. */
  description?: ReactNode;
  /** The primary action on the catalogue: get one. */
  action?: LibraryAction;
  /** The primary action on the sources: add one. */
  sourcesAction?: LibraryAction;
  /** How many sources there are, shown as the tab's count. */
  sourcesCount?: number;
  catalogue: ReactNode;
  sources: ReactNode;
}

export function sectionForHash(hash: string): LibrarySection {
  return hash.replace(/^#/, "") === "sources" ? "sources" : "catalogue";
}

export default function LibraryKindPage({
  title,
  description,
  action,
  sourcesAction,
  sourcesCount,
  catalogue,
  sources,
}: LibraryKindPageProps) {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const section = sectionForHash(location.hash);
  const primary = section === "sources" ? sourcesAction : action;

  const select = (id: string) => {
    navigate({ pathname: location.pathname, hash: id === "sources" ? "sources" : "" }, { replace: true });
  };

  return (
    <div>
      <PageHeader
        eyebrow={t("nav.library")}
        title={title}
        description={description}
        actions={
          primary ? (
            <Button variant="primary" icon={primary.icon} onClick={primary.onClick}>
              {primary.label}
            </Button>
          ) : undefined
        }
      />
      <Tabs
        variant="sections"
        label={t("library.sectionsLabel")}
        value={section}
        onChange={select}
        className="mb-4"
        tabs={[
          { id: "catalogue", label: t("library.catalogue") },
          { id: "sources", label: t("library.sources"), count: sourcesCount },
        ]}
      />
      {/* Only the section on screen is mounted: a catalogue opens streams and
          asks every node questions, and nobody is reading it from Sources. */}
      <div role="tabpanel">{section === "sources" ? sources : catalogue}</div>
    </div>
  );
}
