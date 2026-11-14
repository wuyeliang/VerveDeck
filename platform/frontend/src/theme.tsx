import React, { createContext, useContext, useEffect, useState } from 'react'
import { ConfigProvider, theme as antTheme } from 'antd'

export type ThemeMode = 'light' | 'dark' | 'system'

interface ThemeCtx {
  mode: ThemeMode
  dark: boolean
  setMode: (m: ThemeMode) => void
}

const Ctx = createContext<ThemeCtx>({ mode: 'light', dark: false, setMode: () => {} })
export const useTheme = () => useContext(Ctx)

const KEY = 'vd-theme'
const isMode = (v: any): v is ThemeMode => v === 'light' || v === 'dark' || v === 'system'

/** 主题 Provider：持有 light/dark/system 状态，渲染 antd 主题 ConfigProvider。
 *  内部可再嵌套一个只设 locale 的 ConfigProvider（antd 会合并）。 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>(() => {
    const v = localStorage.getItem(KEY)
    return isMode(v) ? v : 'light'
  })
  const [sysDark, setSysDark] = useState<boolean>(() =>
    typeof window !== 'undefined' && window.matchMedia('(prefers-color-scheme: dark)').matches)

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const h = (e: MediaQueryListEvent) => setSysDark(e.matches)
    mq.addEventListener('change', h)
    return () => mq.removeEventListener('change', h)
  }, [])

  const setMode = (m: ThemeMode) => {
    setModeState(m)
    localStorage.setItem(KEY, m)
  }

  const dark = mode === 'dark' || (mode === 'system' && sysDark)

  return (
    <Ctx.Provider value={{ mode, dark, setMode }}>
      <ConfigProvider theme={{
        algorithm: dark ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
        token: { colorPrimary: '#1677ff', borderRadius: 6 },
      }}>
        {children}
      </ConfigProvider>
    </Ctx.Provider>
  )
}
