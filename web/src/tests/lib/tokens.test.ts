/** Every colour a component asks for has to exist.
 *
 * Tailwind v4 generates a utility only for a token the theme defines. Ask for
 * `bg-background` with no `--color-background` and the class is never emitted:
 * no error, no warning, just an element with no background — which is how the
 * login page came to render dark text on nothing and a primary button came to
 * have no label colour. This is the lint that catches it: collect every
 * `bg-/text-/border-/ring-/…` name the source uses, subtract the ones Tailwind
 * ships, and hold the remainder to what `index.css` actually defines.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** Utilities whose suffix is a colour token. */
const PREFIXES = [
  "bg",
  "text",
  "border",
  "ring",
  "from",
  "to",
  "via",
  "fill",
  "stroke",
  "outline",
  "decoration",
  "divide",
  "placeholder",
  "caret",
  "accent",
  "shadow",
];

/* A class starts after whitespace, a quote, a brace, a paren, or a variant's
   colon (`hover:text-danger`) — never after a hyphen, which is what keeps the
   `to-do` inside "nothing-to-do" out of the results. */
const USE = new RegExp(
  `(?<=^|[\\s"'\`{(:])(?:${PREFIXES.join("|")})-([a-z][a-z0-9]*(?:-[a-z0-9]+)*)`,
  "g",
);

/** Suffixes Tailwind ships: its own palette, plus the non-colour ones that
    share these prefixes (sizes, sides, keywords). Nothing here is ours. */
const TAILWIND = new Set([
  // palette
  "slate", "gray", "zinc", "neutral", "stone", "red", "orange", "amber",
  "yellow", "lime", "green", "emerald", "teal", "cyan", "sky", "blue",
  "indigo", "violet", "purple", "fuchsia", "pink", "rose",
  "black", "white", "transparent", "current", "inherit",
  // sizes (text-sm, shadow-lg, ring-2, border-2, rounded-*)
  "xs", "sm", "base", "md", "lg", "xl", "2xl", "3xl", "4xl", "5xl", "6xl",
  "7xl", "8xl", "9xl", "0", "1", "2", "3", "4", "6", "8", "inner", "outline",
  // border/divide sides and their widths
  "t", "b", "l", "r", "x", "y",
  "t-0", "b-0", "l-0", "r-0", "x-0", "y-0",
  "t-2", "b-2", "l-2", "r-2", "x-2", "y-2",
  "t-4", "b-4", "l-4", "r-4", "x-4", "y-4",
  // keywords
  "left", "center", "right", "justify", "start", "end",
  "wrap", "nowrap", "balance", "pretty", "ellipsis", "clip",
  "solid", "dashed", "dotted", "double", "hidden", "none", "auto",
  "top", "bottom", "cover", "contain", "fixed", "local", "scroll",
  "repeat", "no-repeat", "gradient-to-r", "gradient-to-l", "gradient-to-t",
  "gradient-to-b", "gradient-to-br", "gradient-to-tr", "clip-text",
  "inset", "offset-0", "offset-1", "offset-2", "offset-4",
]);

/** Tailwind's palette entries carry a shade — `zinc-900`, `red-500`. */
const SHADE = /^([a-z]+)-(\d{2,3})$/;

function isTailwind(name: string): boolean {
  if (TAILWIND.has(name)) return true;
  const shade = SHADE.exec(name);
  return shade !== null && TAILWIND.has(shade[1]);
}

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (entry === "tests") continue;
      out.push(...sources(path));
    } else if (/\.tsx?$/.test(entry)) {
      out.push(path);
    }
  }
  return out;
}

/** Every `--color-<name>` the stylesheet defines. */
function definedTokens(css: string): Set<string> {
  return new Set([...css.matchAll(/--color-([a-z0-9-]+)\s*:/g)].map((m) => m[1]));
}

describe("colour tokens", () => {
  const css = readFileSync(join(SRC, "index.css"), "utf8");
  const defined = definedTokens(css);

  it("defines the hub palette under its own names", () => {
    for (const name of [
      "bg", "bg2", "surface", "line", "line-strong", "text", "muted",
      "blue", "blue2", "blue-cta", "brand-accent", "good", "warn", "bad",
    ]) {
      expect(defined, `--color-${name} is missing`).toContain(name);
    }
  });

  it("keeps the names the components already use", () => {
    for (const name of [
      "surface-hover", "border", "border-hover", "primary", "primary-hover",
      "accent-2", "tag-bg", "success", "warning", "danger", "text-muted",
    ]) {
      expect(defined, `--color-${name} is missing`).toContain(name);
    }
  });

  it("defines every colour the source asks for", () => {
    const used = new Map<string, string>();
    for (const file of sources(SRC)) {
      for (const match of readFileSync(file, "utf8").matchAll(USE)) {
        if (!used.has(match[1])) used.set(match[1], file.slice(SRC.length + 1));
      }
    }

    const undefinedTokens = [...used]
      .filter(([name]) => !isTailwind(name) && !defined.has(name))
      .map(([name, file]) => `${name} (first used in ${file})`)
      .sort();

    expect(
      undefinedTokens,
      "these colour utilities name a token index.css does not define, so " +
        "Tailwind emits no class at all and the element renders unstyled",
    ).toEqual([]);
  });
});
