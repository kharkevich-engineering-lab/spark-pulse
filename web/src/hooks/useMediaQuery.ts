/** Which layout the viewport is actually in, as a value a component can read.
 *
 * Tailwind can hide a wide table and show a card list, but that leaves both in
 * the DOM: two buttons named "Delete acme/plain-7b", two sets of labels, and a
 * screen reader reading the page twice. A table and a card list are not two
 * skins of one layout — they are different markup — so the component picks
 * one, and only one is ever rendered.
 *
 * 900px is the brief's break: the same place the header folds its nav.
 */

import { useEffect, useState } from "react";

function evaluate(query: string): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return true;
  return window.matchMedia(query).matches;
}

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => evaluate(query));

  useEffect(() => {
    const mql = window.matchMedia?.(query);
    if (!mql) return;
    setMatches(mql.matches);
    const listener = () => setMatches(mql.matches);
    mql.addEventListener("change", listener);
    return () => mql.removeEventListener("change", listener);
  }, [query]);

  return matches;
}

/** True where a table fits; false on the phone, where rows become cards. */
export function useWideLayout(): boolean {
  return useMediaQuery("(min-width: 900px)");
}

export default useMediaQuery;
