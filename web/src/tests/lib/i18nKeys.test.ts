/** The dictionaries and the source have to agree, in both directions.
 *
 * `i18n.test.tsx` holds the two languages to the same shape. This holds that
 * shape to the code: a key the source asks for and no dictionary carries
 * renders as its own key on screen, and a key no source asks for is a string
 * somebody translated twice and nobody reads. Both are silent failures — the
 * first only on the page nobody opened, the second forever — so they are lints
 * rather than something to notice.
 *
 * Keys reach `t()` two ways and both are counted. Most are literals at the call
 * site. Some are held in a table first (`NodeState`'s conditions, the settings
 * tabs) and some are built from a value (`` t(`status.${key}`) ``), so a bare
 * dotted literal anywhere in the source counts as a use, and a template counts
 * as a use of every key it could name. That direction is deliberately generous:
 * a wrong "this is used" leaves a dead key, a wrong "this is unused" deletes a
 * string somebody is reading.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import en from "@/i18n/en.json";
import fr from "@/i18n/fr.json";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** Every dotted key in a nested dictionary. */
function keysOf(node: unknown, prefix = ""): string[] {
  if (node === null || typeof node !== "object") return [prefix];
  return Object.entries(node as Record<string, unknown>).flatMap(([k, v]) =>
    keysOf(v, prefix ? `${prefix}.${k}` : k),
  );
}

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (entry === "tests" || entry === "i18n") continue;
      out.push(...sources(path));
    } else if (/\.tsx?$/.test(entry)) {
      out.push(path);
    }
  }
  return out;
}

/** `t("a.b")`, `plural("a.b", n)`, `translate("fr", "a.b")` — the key has to be
 *  dotted, which is also what keeps `translate("en", key)`'s language out. */
const CALL = /\b(?:t|plural|translate)\(\s*(?:"(?:en|fr)"\s*,\s*)?"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)"/g;
/** The base a `plural()` call names — the dictionary carries `.one`/`.other`. */
const PLURAL = /\bplural\(\s*"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)"/g;
/** Any dotted literal, for the keys that travel through a table first. */
const LITERAL = /"([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)"/g;
/** A key assembled from a value, as the template it was written as. */
const TEMPLATE = /\b(?:t|plural|translate)\(\s*(?:"(?:en|fr)"\s*,\s*)?`([^`\n]+)`/g;

const englishKeys = new Set(keysOf(en));
const frenchKeys = new Set(keysOf(fr));

const called = new Map<string, string>();
const pluralBases = new Set<string>();
const literals = new Set<string>();
const patterns: RegExp[] = [];

for (const file of sources(SRC)) {
  const text = readFileSync(file, "utf8");
  const where = file.slice(SRC.length + 1);
  for (const m of text.matchAll(CALL)) if (!called.has(m[1])) called.set(m[1], where);
  for (const m of text.matchAll(PLURAL)) pluralBases.add(m[1]);
  for (const m of text.matchAll(LITERAL)) literals.add(m[1]);
  for (const m of text.matchAll(TEMPLATE)) {
    const source = m[1]
      .split(/(\$\{[^}]*\})/)
      .filter(Boolean)
      .map((part) =>
        part.startsWith("${") ? "[A-Za-z0-9_]+" : part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
      )
      .join("");
    patterns.push(new RegExp(`^${source}$`));
  }
}

/** What the source could be asking for, counted generously. */
const used = new Set<string>();
for (const key of literals) {
  used.add(key);
  // A plural is named by its base; the dictionary carries the two forms.
  if (pluralBases.has(key) || englishKeys.has(`${key}.one`)) {
    used.add(`${key}.one`);
    used.add(`${key}.other`);
  }
}

/** What has to exist, counted strictly: only keys handed to `t()` itself. */
const required = new Map<string, string>();
for (const [key, where] of called) {
  if (pluralBases.has(key)) {
    required.set(`${key}.one`, where);
    required.set(`${key}.other`, where);
  } else {
    required.set(key, where);
  }
}

describe("the dictionaries and the source", () => {
  it("carry every key the source asks for, in both languages", () => {
    const missing = [...required]
      .filter(([key]) => !englishKeys.has(key) || !frenchKeys.has(key))
      .map(([key, where]) => `${key} (${where})`);

    expect(missing).toEqual([]);
  });

  /** A key nothing reads is a string translated twice and shown never — and
   *  the only way to find one is to look, which is what this does. */
  it("carry no key the source never asks for", () => {
    const unused = [...englishKeys].filter(
      (key) => !used.has(key) && !patterns.some((p) => p.test(key)),
    );

    expect(unused.sort()).toEqual([]);
  });
});
