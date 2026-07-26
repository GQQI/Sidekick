import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  applyTheme,
  loadLocale,
  loadTheme,
  saveLocale,
  saveTheme,
  translate,
  type Locale,
  type MsgKey,
  type Theme,
} from "./i18n";

type PrefsCtx = {
  locale: Locale;
  theme: Theme;
  setLocale: (locale: Locale) => void;
  setTheme: (theme: Theme) => void;
  t: (key: MsgKey, ...args: string[]) => string;
};

const Ctx = createContext<PrefsCtx | null>(null);

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => loadLocale());
  const [theme, setThemeState] = useState<Theme>(() => {
    const t = loadTheme();
    applyTheme(t);
    return t;
  });

  const setLocale = useCallback((next: Locale) => {
    saveLocale(next);
    setLocaleState(next);
  }, []);

  const setTheme = useCallback((next: Theme) => {
    saveTheme(next);
    applyTheme(next);
    setThemeState(next);
  }, []);

  const t = useCallback(
    (key: MsgKey, ...args: string[]) => translate(locale, key, ...args),
    [locale],
  );

  const value = useMemo(
    () => ({ locale, theme, setLocale, setTheme, t }),
    [locale, theme, setLocale, setTheme, t],
  );

  return createElement(Ctx.Provider, { value }, children);
}

export function usePrefs() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("usePrefs must be used within PrefsProvider");
  return ctx;
}
