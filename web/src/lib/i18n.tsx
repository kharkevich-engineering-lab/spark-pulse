/** Translation, the same shape kharkevich.com uses.
 *
 * Nested JSON per language, addressed by dotted key, with the choice kept in
 * `localStorage`. What differs is only what has to: that site substitutes at
 * build time and ships one HTML file per language, which a single-page app
 * cannot do — the language has to be switchable without a reload, so the
 * dictionaries are bundled and selection happens at runtime.
 *
 * **A missing key renders its own key, and says so once in the console.**
 * Not the English fallback: a French page silently showing English reads as a
 * translation someone chose, and nothing ever reports it. `nav.recipes`
 * appearing on screen is unmistakable, and it is the string a developer greps
 * for.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import en from "@/i18n/en.json";
import fr from "@/i18n/fr.json";

/** The languages this build carries. */
export const LANGUAGES = [
  { id: "en", label: "English", endonym: "English" },
  { id: "fr", label: "French", endonym: "Français" },
] as const;

export type Language = (typeof LANGUAGES)[number]["id"];

const DICTIONARIES: Record<Language, unknown> = { en, fr };

const STORAGE_KEY = "spark-pulse-lang";

/** Values a translation may interpolate. */
export type Vars = Record<string, string | number>;

function isLanguage(value: unknown): value is Language {
  return LANGUAGES.some((l) => l.id === value);
}

/** The stored choice, else the browser's, else English.
 *
 * The browser's own preference is consulted before falling back, because an
 * operator whose machine is set to French should not have to find a setting to
 * be addressed in it.
 */
export function detectLanguage(): Language {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (isLanguage(stored)) return stored;
  } catch {
    // A private window, or site data turned off.
  }
  const preferred = typeof navigator === "undefined" ? [] : (navigator.languages ?? []);
  for (const tag of preferred) {
    const base = tag.split("-")[0];
    if (isLanguage(base)) return base;
  }
  return "en";
}

const warned = new Set<string>();

/** Walk a dotted key into a dictionary. */
function lookup(dictionary: unknown, key: string): string | undefined {
  let node: unknown = dictionary;
  for (const part of key.split(".")) {
    if (node === null || typeof node !== "object") return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

/** Substitute `{name}` placeholders. A missing variable is left visible. */
function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}

export function translate(language: Language, key: string, vars?: Vars): string {
  const value = lookup(DICTIONARIES[language], key);
  if (value === undefined) {
    const id = `${language}:${key}`;
    if (!warned.has(id)) {
      warned.add(id);
      // Reported once per key: a translation gap is a bug to fix, not a
      // condition to live with, and a per-render log would bury it.
      console.warn(`[i18n] no ${language} translation for "${key}"`);
    }
    return key;
  }
  return interpolate(value, vars);
}

/** Pick the plural form for `count` and translate it.
 *
 * `key.one` when there is exactly one, `key.other` otherwise, with `{count}`
 * available to both. English and French agree on this split, which is why two
 * forms are enough for the languages this build carries — a language with more
 * (Polish, Russian, Arabic) would need its own rule here rather than another
 * key, and that is a deliberate thing to notice when one is added.
 */
export function translatePlural(
  language: Language,
  key: string,
  count: number,
  vars?: Vars,
): string {
  return translate(language, `${key}.${count === 1 ? "one" : "other"}`, { count, ...vars });
}

interface I18nValue {
  language: Language;
  setLanguage: (next: Language) => void;
  t: (key: string, vars?: Vars) => string;
  /** `t`, for a string whose wording depends on how many there are. */
  plural: (key: string, count: number, vars?: Vars) => string;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(detectLanguage);

  useEffect(() => {
    // The document says which language it is in — screen readers announce it,
    // and the browser's own translation offer stops fighting the page.
    document.documentElement.lang = language;
  }, [language]);

  const setLanguage = useCallback((next: Language) => {
    setLanguageState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Not worth reporting: the choice still applies to this session.
    }
  }, []);

  const t = useCallback((key: string, vars?: Vars) => translate(language, key, vars), [language]);
  const plural = useCallback(
    (key: string, count: number, vars?: Vars) => translatePlural(language, key, count, vars),
    [language],
  );

  const value = useMemo(
    () => ({ language, setLanguage, t, plural }),
    [language, setLanguage, t, plural],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

/** The translator, the current language, and the way to change it. */
export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (value) return value;
  // Outside a provider — a component rendered in isolation by a test, say.
  // Translating in English beats throwing: a missing provider must not be the
  // reason a page fails to render.
  return {
    language: "en",
    setLanguage: () => {},
    t: (key: string, vars?: Vars) => translate("en", key, vars),
    plural: (key: string, count: number, vars?: Vars) =>
      translatePlural("en", key, count, vars),
  };
}

/** Shorthand for the common case: `const t = useT()`. */
export function useT(): (key: string, vars?: Vars) => string {
  return useI18n().t;
}
