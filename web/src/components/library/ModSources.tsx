/** Where mods come from. Read-only: a mod is a directory of files, and there is
 * nothing to enable, test or remove — only where it is and how many are there.
 */

import { useI18n } from "@/lib/i18n";
import { Section, Table, Td, Th, THead, Tr } from "@/ui";

export interface ModSourcesProps {
  bundled: number;
  custom: number;
}

export default function ModSources({ bundled, custom }: ModSourcesProps) {
  const { t } = useI18n();
  return (
    <Section flush>
      <Table data-testid="mod-sources-table">
        <THead>
          <Th>{t("recipeSources.colSource")}</Th>
          <Th numeric>{t("mods.colMods")}</Th>
        </THead>
        <tbody>
          <Tr data-testid="mod-source-bundled">
            <Td>
              <span className="font-mono font-semibold">{t("recipes.sourceBundled")}</span>
              <p className="text-[12px] text-muted">{t("modSources.bundledWhere")}</p>
            </Td>
            <Td numeric>{bundled}</Td>
          </Tr>
          <Tr data-testid="mod-source-custom">
            <Td>
              <span className="font-mono font-semibold">{t("recipes.sourceCustom")}</span>
              <p className="text-[12px] text-muted">{t("modSources.customWhere")}</p>
            </Td>
            <Td numeric>{custom}</Td>
          </Tr>
        </tbody>
      </Table>
    </Section>
  );
}
