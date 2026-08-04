import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import {
  LanguageProvider,
  useLanguage,
  useT,
  resolveLanguage,
  resolvePref,
  LANG_KEY,
  detectLanguage,
} from "./index";

function Probe() {
  const { lang, pref, setLanguage } = useLanguage();
  const t = useT();
  return (
    <div>
      <span data-testid="lang">{lang}</span>
      <span data-testid="pref">{pref}</span>
      <span data-testid="hello">{t("Hello")}</span>
      <button onClick={() => setLanguage("en")}>to-en</button>
      <button onClick={() => setLanguage("zh")}>to-zh</button>
      <button onClick={() => setLanguage("auto")}>to-auto</button>
    </div>
  );
}

const renderProbe = () =>
  render(
    <LanguageProvider>
      <Probe />
    </LanguageProvider>,
  );

describe("i18n manual language override", () => {
  const realNav = Object.getOwnPropertyDescriptor(window.navigator, "language");

  beforeEach(() => {
    localStorage.clear();
    // force a non-zh system language so "auto" resolves to English
    Object.defineProperty(window.navigator, "language", {
      configurable: true,
      get: () => "en-US",
    });
  });

  afterEach(() => {
    cleanup();
    if (realNav) Object.defineProperty(window.navigator, "language", realNav);
    localStorage.clear();
  });

  it("detectLanguage follows the system; resolveLanguage reads the override", () => {
    Object.defineProperty(window.navigator, "language", {
      configurable: true,
      get: () => "zh-CN",
    });
    expect(detectLanguage()).toBe("zh");
    expect(resolvePref()).toBe("auto");
    expect(resolveLanguage()).toBe("zh");

    localStorage.setItem(LANG_KEY, "en");
    expect(resolvePref()).toBe("en");
    expect(resolveLanguage()).toBe("en"); // override beats system
  });

  it("starts on the system language with pref=auto", () => {
    renderProbe();
    expect(screen.getByTestId("lang").textContent).toBe("en");
    expect(screen.getByTestId("pref").textContent).toBe("auto");
    // en dictionary: missing key renders the English source
    expect(screen.getByTestId("hello").textContent).toBe("Hello");
  });

  it("switching to zh re-renders through the zh dictionary and persists", () => {
    renderProbe();
    act(() => screen.getByText("to-zh").click());
    expect(screen.getByTestId("lang").textContent).toBe("zh");
    expect(screen.getByTestId("pref").textContent).toBe("zh");
    expect(screen.getByTestId("hello").textContent).toBe("你好");
    expect(localStorage.getItem(LANG_KEY)).toBe("zh");
  });

  it("switching back to auto follows the system again", () => {
    renderProbe();
    act(() => screen.getByText("to-zh").click());
    act(() => screen.getByText("to-auto").click());
    expect(screen.getByTestId("pref").textContent).toBe("auto");
    expect(screen.getByTestId("lang").textContent).toBe("en"); // system is en-US
    expect(localStorage.getItem(LANG_KEY)).toBe("auto");
  });

  it("a persisted override is honored on mount", () => {
    localStorage.setItem(LANG_KEY, "zh");
    renderProbe();
    expect(screen.getByTestId("lang").textContent).toBe("zh");
    expect(screen.getByTestId("pref").textContent).toBe("zh");
  });
});
