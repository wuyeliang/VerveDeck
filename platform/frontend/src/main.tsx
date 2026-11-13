import React, { useEffect, useState } from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import { useTranslation } from 'react-i18next'
import zhCN from 'antd/locale/zh_CN'
import zhTW from 'antd/locale/zh_TW'
import jaJP from 'antd/locale/ja_JP'
import enUS from 'antd/locale/en_US'
import frFR from 'antd/locale/fr_FR'
import esES from 'antd/locale/es_ES'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import 'dayjs/locale/zh-tw'
import 'dayjs/locale/ja'
import 'dayjs/locale/en'
import 'dayjs/locale/fr'
import 'dayjs/locale/es'
import './i18n'
import App from './App'

const ANT_LOCALES: Record<string, any> = { 'zh-CN': zhCN, 'zh-TW': zhTW, ja: jaJP, en: enUS, fr: frFR, es: esES }
const DAY_LOCALES: Record<string, string> = { 'zh-CN': 'zh-cn', 'zh-TW': 'zh-tw', ja: 'ja', en: 'en', fr: 'fr', es: 'es' }

function LocaleBridge({ children }: { children: React.ReactNode }) {
  const { i18n: t } = useTranslation()
  const [lang, setLangState] = useState(t.language)
  useEffect(() => {
    const onLang = (l: string) => setLangState(l)
    t.on('languageChanged', onLang)
    return () => { t.off('languageChanged', onLang) }
  }, [t])
  useEffect(() => { dayjs.locale(DAY_LOCALES[lang] || 'zh-cn') }, [lang])
  return <ConfigProvider locale={ANT_LOCALES[lang] || zhCN} theme={{ token: { colorPrimary: '#1677ff', borderRadius: 6 } }}>{children}</ConfigProvider>
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <LocaleBridge>
      <App />
    </LocaleBridge>
  </React.StrictMode>,
)
