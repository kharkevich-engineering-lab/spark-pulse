import { Copyright } from "lucide-react";
import { SiGithub, SiPypi } from "@icons-pack/react-simple-icons";
import logoUrl from "@/assets/kharkevich-logo.svg";

/** Who made this, on the two pages that stand outside the application.
 *
 * The sidebar carries this on every page inside `Layout`. Login and the
 * not-found page render outside it, so they had no attribution at all — the
 * two pages most likely to be someone's *first* view of the product, and the
 * one place a visitor has no other way to tell whose software this is.
 */
export function BrandFooter() {
  return (
    <footer className="p-6 text-center text-xs text-text-muted space-y-3">
      <a
        href="https://kharkevich.com"
        target="_blank"
        rel="noopener"
        className="inline-flex flex-col items-center gap-2 hover:text-text transition-colors"
      >
        <img
          src={logoUrl}
          alt="Kharkevich Engineering Lab"
          className="h-10 w-10 opacity-80"
          width={40}
          height={40}
        />
        <span className="inline-flex items-center gap-1.5">
          <Copyright size={12} />
          {new Date().getFullYear()} Kharkevich Engineering Lab
        </span>
      </a>
      <div className="flex items-center justify-center gap-4">
        <a
          href="https://github.com/kharkevich-engineering-lab/spark-pulse"
          target="_blank"
          rel="noopener"
          className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
        >
          <SiGithub size={12} />
          GitHub
        </a>
        <a
          href="https://pypi.org/project/spark-pulse/"
          target="_blank"
          rel="noopener"
          className="inline-flex items-center gap-1.5 hover:text-text transition-colors"
        >
          <SiPypi size={12} />
          PyPI
        </a>
      </div>
    </footer>
  );
}
