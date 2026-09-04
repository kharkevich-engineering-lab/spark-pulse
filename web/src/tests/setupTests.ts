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

// Mock matchMedia
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query) => ({
    matches: false,
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
