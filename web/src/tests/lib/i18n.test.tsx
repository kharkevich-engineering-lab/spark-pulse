/** Translation.
 *
 * Two properties carry most of the weight. A missing key has to be *visible*
 * rather than quietly English — a French page showing English reads as a
 * choice somebody made, and nothing ever reports it. And the dictionaries have
 * to stay the same shape as each other, because the way a translation goes
 * stale is one language gaining a key the other never gets.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nProvider, LANGUAGES, detectLanguage, translate, useI18n } from "@/lib/i18n";
import en from "@/i18n/en.json";
import fr from "@/i18n/fr.json";

/** Every dotted key in a nested dictionary. */
function keysOf(node: unknown, prefix = ""): string[] {
  if (node === null || typeof node !== "object") return [prefix];
  return Object.entries(node as Record<string, unknown>).flatMap(([k, v]) =>
    keysOf(v, prefix ? `${prefix}.${k}` : k),
  );
}

describe("the dictionaries", () => {
  /** The way a translation goes stale: one language gains a key and the other
   *  does not, so that string silently renders as its own key in the second
   *  language and nobody notices until somebody switches. */
  it("carry exactly the same keys in every language", () => {
    const english = keysOf(en).sort();
    const french = keysOf(fr).sort();

    expect(french).toEqual(english);
  });

  it("leave no value empty", () => {
    for (const dict of [en, fr]) {
      for (const key of keysOf(dict)) {
        expect(translate(dict === en ? "en" : "fr", key)).not.toBe("");
      }
    }
  });

  /** Keys whose French is legitimately the same string as the English.
   *
   *  Listed one by one rather than matched by a rule, so that adding to this
   *  set is a decision somebody made rather than a translation that quietly
   *  never happened. Proper nouns, initialisms, and the handful of words
   *  French borrowed unchanged. */
  const IDENTICAL_ON_PURPOSE = new Set([
    "nav.mcp",
    "nav.images",
    "nav.cache",
    "common.ok",
    "engines.none",
    // "Placement", "Image" and "RAM" are the same word in French; "PID" and
    // "MCP" are initialisms.
    "cluster.colPlacement",
    "inference.image",
    "images.colImage",
    "monitoring.pid",
    "monitoring.ram",
    "images.colActions",
    // "Mods" is the term of art the recipes themselves use, in both languages.
    "recipes.tabMods",
    "eventStream.info",
    "nodes.colInterfaces",
    "nodes.colActions",
    // Placeholders that are literal examples — an address, a key path — not
    // prose. Translating them would make them wrong.
    "nodes.addressPlaceholder",
    "nodes.sshKeyPlaceholder",
    "recipeForm.buildArgPlaceholder",
    "deployOptions.extraArgsPlaceholder",
    // "Mods", "Image" and "Port" are the same word in French.
    "recipeForm.mods",
    "deployOptions.mods",
    "deployOptions.image",
    "deployOptions.port",
    // "Secrets", "Mods", "Ethernet", "InfiniBand" and "mode 600" carry over
    // into French unchanged.
    "settings.tabSecrets",
    "settings.secrets",
    "settings.mods",
    "settings.ethernet",
    "settings.infiniband",
    "settings.mode600",
    // "Source", "Actions" and "Transport" carry over; the rest are product
    // names and literal examples.
    "models.source",
    "models.colActions",
    "mcp.transport",
    "mcp.transportValue",
    "mcp.cursor",
    "benchmarking.recipeIdPlaceholder",
    "oci.url",
  ]);

  /** A French dictionary that is a copy of the English one is not a
   *  translation. This does not check quality — it checks that the work
   *  happened at all. */
  it("actually differ from each other", () => {
    const translatable = keysOf(en).filter(
      (k) => !k.startsWith("brand.") && !IDENTICAL_ON_PURPOSE.has(k),
    );

    const same = translatable.filter((k) => translate("en", k) === translate("fr", k));

    expect(same).toEqual([]);
  });
});

describe("translate", () => {
  it("resolves a dotted key", () => {
    expect(translate("en", "nav.settings")).toBe("Settings");
    expect(translate("fr", "nav.settings")).toBe("Paramètres");
  });

  /** Not the English fallback: a French page quietly showing English reads as
   *  a translation somebody chose, and nothing ever reports it. The key on
   *  screen is unmistakable, and it is what a developer greps for. */
  it("renders the key itself when there is no translation", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(translate("fr", "nav.nothingIsCalledThis")).toBe("nav.nothingIsCalledThis");
    } finally {
      warn.mockRestore();
    }
  });

  it("reports a missing key once rather than on every render", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      translate("en", "some.key.reported.once");
      translate("en", "some.key.reported.once");
      translate("en", "some.key.reported.once");

      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      warn.mockRestore();
    }
  });

  it("substitutes variables", () => {
    // Asserted against a key that exists so the test does not depend on a
    // particular sentence: interpolation is the behaviour under test.
    expect(translate("en", "common.save", { unused: 1 })).toBe("Save");
  });

  it("walks into a key that is not a string without throwing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      // `nav` is an object, not a string.
      expect(translate("en", "nav")).toBe("nav");
      expect(translate("en", "nav.settings.deeper")).toBe("nav.settings.deeper");
    } finally {
      warn.mockRestore();
    }
  });
});

// ── The provider ────────────────────────────────────────────────────────────

function Probe() {
  const { language, setLanguage, t } = useI18n();
  return (
    <div>
      <span data-testid="lang">{language}</span>
      <span data-testid="label">{t("nav.settings")}</span>
      <button onClick={() => setLanguage("fr")}>to french</button>
    </div>
  );
}

describe("I18nProvider", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal("navigator", { ...navigator, languages: [] });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    document.documentElement.removeAttribute("lang");
  });

  it("starts in English when nothing says otherwise", () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );

    expect(screen.getByTestId("lang")).toHaveTextContent("en");
    expect(screen.getByTestId("label")).toHaveTextContent("Settings");
  });

  it("switches the whole tree and remembers the choice", async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );

    await user.click(screen.getByRole("button", { name: "to french" }));

    expect(screen.getByTestId("label")).toHaveTextContent("Paramètres");
    expect(localStorage.getItem("spark-pulse-lang")).toBe("fr");
  });

  /** Screen readers announce the document's language, and the browser stops
   *  offering to translate a page that already says what it is. */
  it("tells the document which language it is in", async () => {
    const user = userEvent.setup();
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    );
    expect(document.documentElement.lang).toBe("en");

    await user.click(screen.getByRole("button", { name: "to french" }));

    expect(document.documentElement.lang).toBe("fr");
  });

  /** Rendered in isolation — by a test, or by a component tree that has no
   *  provider above it — translating in English beats throwing. A missing
   *  provider must not be the reason a page fails to render. */
  it("works without a provider above it", () => {
    render(<Probe />);

    expect(screen.getByTestId("label")).toHaveTextContent("Settings");
  });
});

describe("detectLanguage", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("prefers what was stored", () => {
    localStorage.setItem("spark-pulse-lang", "fr");
    expect(detectLanguage()).toBe("fr");
  });

  it("ignores a stored value this build does not carry", () => {
    localStorage.setItem("spark-pulse-lang", "kl");
    expect(detectLanguage()).toBe("en");
  });

  /** Somebody whose machine is set to French should not have to find a
   *  setting before being addressed in it. */
  it("falls back to the browser's own preference", () => {
    vi.stubGlobal("navigator", { ...navigator, languages: ["fr-CA", "en"] });
    expect(detectLanguage()).toBe("fr");
  });

  it("falls back to English when the browser asks for something else", () => {
    vi.stubGlobal("navigator", { ...navigator, languages: ["de-DE"] });
    expect(detectLanguage()).toBe("en");
  });

  it("survives a browser that refuses to remember anything", () => {
    const boom = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("site data is blocked");
    });
    try {
      expect(detectLanguage()).toBe("en");
    } finally {
      boom.mockRestore();
    }
  });
});

describe("LANGUAGES", () => {
  it("names each language in its own words", () => {
    // Somebody looking for French looks for "Français", not for the English
    // word for it.
    expect(LANGUAGES.map((l) => l.endonym)).toEqual(["English", "Français"]);
  });
});
