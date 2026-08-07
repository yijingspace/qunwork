/**
 * QunWork lightweight i18n — language auto-follows the OS / browser language,
 * with a manual override persisted in localStorage (`qunwork-lang`:
 * "auto" | "zh" | "en").
 *
 * Design choices:
 * - No external i18n dependency: a small React context + per-language dictionary.
 * - Dictionary keys are the English source strings themselves, so replacing UI
 *   copy is `t("Original text")` with no separate key naming step.
 * - Detection: `navigator.language` (WebView2/browser follows the system
 *   language). zh* → Chinese, anything else → English (fallback).
 * - Override: `setLanguage("zh" | "en" | "auto")` stores the preference and
 *   re-renders immediately; "auto" (or unset) follows the system again.
 */

import React, { createContext, useCallback, useContext, useMemo, useState } from "react";
import { zh, en, Messages } from "./messages";

export type Lang = "zh" | "en";
export type LangPref = Lang | "auto";

export const LANG_KEY = "qunwork-lang";

/** Detect the UI language from the host (OS/WebView) language. */
export function detectLanguage(): Lang {
  try {
    const lang = (navigator.language || "en").toLowerCase();
    return lang.startsWith("zh") ? "zh" : "en";
  } catch {
    return "en";
  }
}

/** Resolve the effective language: localStorage override wins, else system. */
export function resolveLanguage(): Lang {
  try {
    const pref = localStorage.getItem(LANG_KEY);
    if (pref === "zh" || pref === "en") return pref;
  } catch {
    // storage unavailable (tests/private mode) — fall through to system
  }
  return detectLanguage();
}

/** Resolve the stored preference ("auto" when unset/invalid). */
export function resolvePref(): LangPref {
  try {
    const pref = localStorage.getItem(LANG_KEY);
    if (pref === "zh" || pref === "en" || pref === "auto") return pref;
  } catch {
    // ignore storage failures
  }
  return "auto";
}

export interface I18n {
  lang: Lang;
  pref: LangPref;
  setLanguage: (pref: LangPref) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const NOOP = () => {};

const LanguageContext = createContext<I18n>({
  lang: "en",
  pref: "auto",
  setLanguage: NOOP,
  t: (key: string, vars?: Record<string, string | number>) =>
    vars ? key.replace(/\{(\w+)\}/g, (_, k: string) => String(vars[k] ?? "")) : key,
});

export const useLanguage = (): I18n => useContext(LanguageContext);
export const useT = (): I18n["t"] => useContext(LanguageContext).t;

/** Synchronize <html lang> with the active UI language (a11y: screen readers
 * and search engines rely on it). Owner-audit 2026-08-07. */
function syncHtmlLang(lang: Lang): void {
  try {
    document.documentElement.lang = lang;
  } catch {
    // non-DOM environment (tests) — ignore
  }
}

export const LanguageProvider = ({ children }: { children: React.ReactNode }) => {
  const [lang, setLang] = useState<Lang>(() => {
    const initial = resolveLanguage();
    syncHtmlLang(initial);
    return initial;
  });
  const [pref, setPref] = useState<LangPref>(resolvePref);
  const dict: Messages = lang === "zh" ? zh : en;

  const setLanguage = useCallback((p: LangPref) => {
    try {
      localStorage.setItem(LANG_KEY, p);
    } catch {
      // ignore storage failures — in-memory switch still applies
    }
    setPref(p);
    const next = p === "auto" ? detectLanguage() : p;
    syncHtmlLang(next);
    setLang(next);
  }, []);

  const value = useMemo<I18n>(
    () => ({
      lang,
      pref,
      setLanguage,
      t: (key: string, vars?: Record<string, string | number>) => {
        const s = dict[key] ?? en[key] ?? key;
        if (!vars) return s;
        return s.replace(/\{(\w+)\}/g, (_, k: string) => String(vars[k] ?? ""));
      },
    }),
    [lang, pref, dict, setLanguage],
  );

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
};
