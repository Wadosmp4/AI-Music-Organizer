import "@testing-library/jest-dom/vitest";

// This project's jsdom/vitest combination doesn't expose a working
// localStorage (both `localStorage` and `window.localStorage` are
// `undefined` even with a real http(s) environment URL configured) --
// a minimal in-memory Storage polyfill so tests can exercise the reorganize
// session persistence Onboarding relies on.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }

  clear(): void {
    this.store.clear();
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

if (typeof globalThis.localStorage === "undefined") {
  Object.defineProperty(globalThis, "localStorage", { value: new MemoryStorage() });
}
if (typeof window !== "undefined" && typeof window.localStorage === "undefined") {
  Object.defineProperty(window, "localStorage", { value: globalThis.localStorage });
}
