/** One line of help, with the rest of the explanation one hover away.
 *
 * Settings carried a paragraph under nearly every field. The first sentence
 * says what the value means, which is what a reader scanning the form needs;
 * the reasons after it are for the one who stops to ask, so they sit behind
 * an `InfoTip` rather than under every control at once.
 */

import { useT } from "@/lib/i18n";
import { InfoTip } from "@/ui";

/** The first sentence, and whatever follows it. */
export function splitBrief(text: string): [string, string] {
  const match = /^(.+?[.!?…])\s+(\S[\s\S]*)$/.exec(text.trim());
  return match ? [match[1], match[2]] : [text, ""];
}

export default function Brief({ text }: { text: string }) {
  const t = useT();
  const [first, rest] = splitBrief(text);
  if (!rest) return <>{text}</>;
  return (
    <>
      {first}{" "}
      <InfoTip label={t("settings.more")}>{text}</InfoTip>
    </>
  );
}
