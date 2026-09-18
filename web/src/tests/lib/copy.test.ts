/** No English in the markup.
 *
 * A page is only translated for as long as nobody adds a line to it, and the
 * failure is invisible from the English side: the French operator sees one
 * sentence in the wrong language and has no way to report it as a bug rather
 * than a choice. So this reads the pages and components the way an operator
 * does — the text between the tags, and the three attributes a screen reader
 * or a tooltip says out loud — and refuses a word that did not come from the
 * dictionary.
 *
 * What is allowed through is listed one entry at a time, not matched by a
 * rule: units, mono values, product names and the punctuation that separates
 * them. Adding to that list is a decision somebody made.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SCANNED = ["pages", "components", "ui"];

/** Text a reader is meant to read, between two tags and with no expression in
 *  it — `{t("…")}` and every other `{…}` is skipped by construction. */
const JSX_TEXT = />([^<>{}]{2,})</g;
/** The attributes that are spoken or shown rather than styled. */
const ATTRIBUTE = /\b(aria-label|title|placeholder)="([^"]{2,})"/g;

/** Prose, rather than a type parameter or half an expression: letters and the
 *  punctuation prose uses, and nothing a compiler would need. */
const PROSE = /^[A-Za-z][A-Za-z0-9 ,.'’–—&:%/!?·-]*$/;

/** What is not a translation, one entry at a time.
 *
 *  Filenames and references (the operator types these and they are the same
 *  in every language), units and symbols, and the product names. */
const ALLOWED = new Set([
  // Filenames and paths the operator sees verbatim.
  "run.sh",
  "claude_desktop_config.json",
  "spark-pulse mcp",
  "~/.config/spark-pulse/settings.json",
  // Literal example values in placeholders — translating one makes it wrong.
  "vllm",
  "https://huggingface.co",
  "/models",
  "~/.cache/vllm",
  "9000",
  "9100",
  "64",
  "4096",
  "1048576",
  // Model-source kinds, as the API spells them.
  "hf_hub",
  "local_path",
  // Product and brand.
  "Spark Pulse",
]);

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if (entry.endsWith(".tsx")) out.push(path);
  }
  return out;
}

/** Block and line comments, which are for the reader of the code. */
function withoutComments(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function findings(file: string): string[] {
  const text = withoutComments(readFileSync(file, "utf8"));
  const where = file.slice(SRC.length + 1);
  const out: string[] = [];

  for (const m of text.matchAll(JSX_TEXT)) {
    const phrase = m[1].replace(/\s+/g, " ").trim();
    if ((phrase.match(/[A-Za-z]/g) ?? []).length < 2) continue;
    if (!PROSE.test(phrase) || ALLOWED.has(phrase)) continue;
    // `=>` followed by a type parameter reads as text between two tags.
    if (text[m.index! - 1] === "=") continue;
    out.push(`${where}: ${phrase}`);
  }

  for (const m of text.matchAll(ATTRIBUTE)) {
    const value = m[2].trim();
    if (ALLOWED.has(value)) continue;
    out.push(`${where}: ${m[1]}="${value}"`);
  }

  return out;
}

describe("the pages and components", () => {
  it("put every word an operator reads through the dictionary", () => {
    const english = SCANNED.flatMap((dir) => sources(join(SRC, dir))).flatMap(findings);

    expect(english.sort()).toEqual([]);
  });
});
