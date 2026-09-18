import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { vi } from "vitest";

// Clean up after each test
afterEach(() => {
  cleanup();
});

// Web Storage. This jsdom build ships none at all — `window.localStorage` is
// `undefined`, so `lib/theme.ts`'s `getTheme()` throws on the first render of
// anything that reads the theme (Layout, SettingsPage, LazyCodeEditor). An
// in-memory Storage is the same kind of missing-browser-API stand-in as the
// ResizeObserver above, and it is cleared between tests so nothing leaks a
// theme (or a dismissed banner) into the next one.
function memoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    key: (i: number) => [...store.keys()][i] ?? null,
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  } as Storage;
}

for (const key of ["localStorage", "sessionStorage"] as const) {
  Object.defineProperty(window, key, {
    configurable: true,
    writable: true,
    value: memoryStorage(),
  });
}

afterEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

// Mock ResizeObserver
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
};

// Mock IntersectionObserver
global.IntersectionObserver = class MockIntersectionObserver implements IntersectionObserver {
  root: Element | null = null;
  rootMargin: string = "0px";
  thresholds: ReadonlyArray<number> = [];
  
  constructor() {}
  observe() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
  unobserve() {}
};

// Mock matchMedia.
//
// A width query is answered from `window.innerWidth` (jsdom's default is
// 1024) rather than with a flat `false`: `useMediaQuery` decides whether a
// page renders a table or a list of cards, and a stub that says "no" to every
// query would silently test the phone layout on every desktop assertion. A
// test that wants the narrow layout sets `window.innerWidth` before rendering.
// Everything else — `prefers-color-scheme`, say — still answers false.
function widthMatches(query: string): boolean {
  const min = /min-width:\s*(\d+)px/.exec(query);
  const max = /max-width:\s*(\d+)px/.exec(query);
  if (!min && !max) return false;
  if (min && window.innerWidth < Number(min[1])) return false;
  if (max && window.innerWidth > Number(max[1])) return false;
  return true;
}

/** Put the viewport back where every other test expects it. */
afterEach(() => {
  Object.defineProperty(window, "innerWidth", { writable: true, configurable: true, value: 1024 });
});

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query) => ({
    matches: widthMatches(query),
    media: query,
    onchange: null,
    addListener: vi.fn(), // deprecated
    removeListener: vi.fn(), // deprecated
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

// Mock EventSource
class MockEventSource {
  url: string;
  _listeners: Record<string, Array<() => void>> = {};
  readyState = 0; // CONNECTING

  constructor(url: string) {
    this.url = url;
  }

  addEventListener(_type: string, _listener: EventListener) {
    if (!this._listeners[_type]) {
      this._listeners[_type] = [];
    }
    this._listeners[_type].push(_listener as () => void);
  }

  removeEventListener(_type: string, _listener: EventListener) {
    if (!this._listeners[_type]) return;
    this._listeners[_type] = this._listeners[_type].filter(
      (l) => l !== _listener
    );
  }

  close() {
    this.readyState = 2; // CLOSED
  }
}

global.EventSource = MockEventSource as any;

// Mock crypto.randomUUID
if (!global.crypto?.randomUUID) {
  (global.crypto as any).randomUUID = () =>
    "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      const v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
}
