import { SiGithub, SiPypi } from "@icons-pack/react-simple-icons";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** Who made this, on every page.
 *
 * It was a stack inside the sidebar on the eleven pages that had one and a
 * centred block on the two that did not, which is two footers. This is the
 * hub's: one row, the copyright on the left, links on the right, a 1px rule
 * above it, stacking to a column under 900px. The lab's name lives here and
 * nowhere else in the shell — the header lockup had it too, which put the
 * same words twice on one screen — and there is no logo and no tagline: the
 * product is not "for DGX Spark" any more, and a footer that repeats the
 * header's mark reads as clutter, not identity.
 */
export function BrandFooter({ className }: { className?: string }) {
  const t = useT();
  return (
    <footer
      className={cn(
        "w-full max-w-[1100px] mx-auto flex flex-col items-start gap-3",
        "px-5 pt-6 pb-8 border-t border-line text-[12px] text-muted",
        "min-[900px]:flex-row min-[900px]:items-center min-[900px]:justify-between",
        "min-[900px]:gap-x-6 min-[900px]:px-7 min-[900px]:pt-7 min-[900px]:pb-10",
        className,
      )}
    >
      <a
        href="https://kharkevich.com"
        target="_blank"
        rel="noopener"
        className="whitespace-nowrap text-muted no-underline hover:text-text hover:no-underline transition-colors"
      >
        © {new Date().getFullYear()} {t("brand.company")}
      </a>
      <div className="flex items-center gap-4">
        <a
          href="https://github.com/kharkevich-engineering-lab/spark-pulse"
          target="_blank"
          rel="noopener"
          className="inline-flex items-center gap-1.5 text-muted no-underline hover:text-text hover:no-underline transition-colors"
        >
          <SiGithub size={12} />
          {t("brand.github")}
        </a>
        <a
          href="https://pypi.org/project/spark-pulse/"
          target="_blank"
          rel="noopener"
          className="inline-flex items-center gap-1.5 text-muted no-underline hover:text-text hover:no-underline transition-colors"
        >
          <SiPypi size={12} />
          {t("brand.pypi")}
        </a>
      </div>
    </footer>
  );
}
