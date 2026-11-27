// Light is the default; dark is opt-in only (the OS setting is ignored on purpose).
export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'trinity-theme'

export function loadTheme(): Theme {
  try { return localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light' } catch { return 'light' }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme
  try { localStorage.setItem(STORAGE_KEY, theme) } catch { /* private mode / blocked storage: theme still applies for this visit */ }
}
