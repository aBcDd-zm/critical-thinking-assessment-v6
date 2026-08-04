import { afterEach, vi } from "vitest";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

Element.prototype.scrollIntoView = vi.fn();

Object.defineProperty(window, "speechSynthesis", {
  configurable: true,
  value: { cancel: vi.fn(), speak: vi.fn(), getVoices: vi.fn(() => []) },
});

class UtteranceMock {
  lang = "";
  rate = 1;
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public text: string) {}
}

Object.defineProperty(window, "SpeechSynthesisUtterance", { configurable: true, value: UtteranceMock });
Object.defineProperty(globalThis, "SpeechSynthesisUtterance", { configurable: true, value: UtteranceMock });

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
