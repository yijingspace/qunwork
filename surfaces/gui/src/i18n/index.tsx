/**
 * QunWork lightweight i18n — language auto-follows the OS / browser language.
 *
 * Design choices:
 * - No external i18n dependency: a small React context + per-language dictionary.
 * - Dictionary keys are the English source strings themselves, so replacing UI
 *   copy is `t("Original text")` with no separate key naming step.
 * - Detection: `navigator.language` (WebView2/browser follows the system
 *   language). zh* → Chinese, anything else → English (fallback).
 */

import React, { createContext, useContext, useMemo, useState } from "react";
import { zh, en, Messages } from "./messages";

export type Lang = "zh" | "en";

/** Detect the UI language from the host (OS/WebView) language. */
export function detectLanguage(): Lang {
  try {
    const lang = (navigator.language || "en").toLowerCase();
    return lang.startsWith("zh") ? "zh" : "en";
  } catch {
    return "en";
  }
}

export interface I18n {
  lang: Lang;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const LanguageContext = createContext<I18n>({
  lang: "en",
  t: (key: string, vars?: Record<string, string | number>) =>
    vars ? key.replace(/\{(\w+)\}/g, (_, k: string) => String(vars[k] ?? "")) : key,
});

export const useLanguage = (): I18n => useContext(LanguageContext);
export const useT = (): I18n["t"] => useContext(LanguageContext).t;

export const LanguageProvider = ({ children }: { children: React.ReactNode }) => {
  const [lang] = useState<Lang>(detectLanguage);
  const dict: Messages = lang === "zh" ? zh : en;

  const value = useMemo<I18n>(
    () => ({
      lang,
      t: (key: string, vars?: Record<string, string | number>) => {
        const s = dict[key] ?? en[key] ?? key;
        if (!vars) return s;
        return s.replace(/\{(\w+)\}/g, (_, k: string) => String(vars[k] ?? ""));
      },
    }),
    [lang, dict],
  );

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
};
