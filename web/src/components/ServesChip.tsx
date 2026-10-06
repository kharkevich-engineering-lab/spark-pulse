/** What a recipe or a run serves, when it is not chat.
 *
 * Chat is the default and says nothing: a chip on every card would be noise on
 * the catalogue's common case. Any other kind is worth a glance, because it
 * changes what the endpoint answers and whether a benchmark can run against
 * it. A kind this build does not know — one a newer control plane recorded —
 * is shown as stored rather than hidden. */

import { Boxes } from "lucide-react";
import { useT } from "@/lib/i18n";
import { cn, servesChat } from "@/lib/utils";

const LABELS: Record<string, string> = {
  embedding: "serves.embedding",
  image: "serves.image",
  video: "serves.video",
  speech: "serves.speech",
};

export default function ServesChip({ serves, className }: { serves?: string | null; className?: string }) {
  const t = useT();
  if (servesChat(serves)) return null;
  const kind = serves as string;
  const key = LABELS[kind];
  return (
    <span
      data-testid="serves-chip"
      title={t("serves.hint")}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border border-primary/30 bg-primary/15 px-2 py-0.5 text-xs text-blue2",
        className,
      )}
    >
      <Boxes size={11} />
      {key ? t(key) : kind}
    </span>
  );
}
