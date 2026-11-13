import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import zhCN from './locales/zh-CN.json'
import zhTW from './locales/zh-TW.json'
import ja from './locales/ja.json'
import en from './locales/en.json'
import fr from './locales/fr.json'
import es from './locales/es.json'

export const LANGS = [
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-TW', label: '繁體中文' },
  { code: 'ja', label: '日本語' },
  { code: 'en', label: 'English' },
  { code: 'fr', label: 'Français' },
  { code: 'es', label: 'Español' },
]

function detect(): string {
  const saved = localStorage.getItem('lang')
  if (saved && LANGS.some((l) => l.code === saved)) return saved
  const nav = navigator.language || 'zh-CN'
  if (nav.startsWith('zh')) return nav.toUpperCase().endsWith('TW') ? 'zh-TW' : 'zh-CN'
  if (nav.startsWith('ja')) return 'ja'
  if (nav.startsWith('fr')) return 'fr'
  if (nav.startsWith('es')) return 'es'
  if (nav.startsWith('en')) return 'en'
  return 'zh-CN'
}

i18n.use(initReactI18next).init({
  resources: {
    'zh-CN': { translation: zhCN },
    'zh-TW': { translation: zhTW },
    ja: { translation: ja },
    en: { translation: en },
    fr: { translation: fr },
    es: { translation: es },
  },
  lng: detect(),
  fallbackLng: 'zh-CN',
  interpolation: { escapeValue: false },
})

export function setLang(code: string) {
  localStorage.setItem('lang', code)
  i18n.changeLanguage(code)
  document.documentElement.lang = code
}

export default i18n
