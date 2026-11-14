import React, { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, useNavigate, useLocation, Navigate } from 'react-router-dom'
import { Layout, Menu, Dropdown, Avatar, Select, Tag, theme as antTheme, Tooltip } from 'antd'
import { useTranslation } from 'react-i18next'
import {
  DashboardOutlined, DatabaseOutlined, ContainerOutlined, ExperimentOutlined,
  BarChartOutlined, CloudServerOutlined, ClusterOutlined, SettingOutlined,
  LogoutOutlined, RocketOutlined, GlobalOutlined, ShrinkOutlined,
  SunOutlined, MoonOutlined, DesktopOutlined, GithubOutlined,
} from '@ant-design/icons'
import { useTheme } from './theme'
import Login from './pages/login'
import Dashboard from './pages/dashboard'
import DataCenter from './pages/data'
import ModelRepo from './pages/models'
import TrainCenter from './pages/train'
import EvalCenter from './pages/evals'
import QuantCenter from './pages/quantize'
import Inference from './pages/infer'
import ClusterOps from './pages/cluster'
import AdminPanel from './pages/admin'
import { api, getProjects } from './api'
import i18n, { LANGS, setLang } from './i18n'

const { Sider, Header, Content } = Layout

function Shell({ children }: { children: React.ReactNode }) {
  const nav = useNavigate()
  const loc = useLocation()
  const { t, i18n: ti18n } = useTranslation()
  const [user, setUser] = useState<any>(null)
  const [projects, setProjects] = useState<any[]>([])
  const [projectId, setProjectId] = useState<number>(Number(localStorage.getItem('projectId') || 0))
  const { token } = antTheme.useToken()
  const { mode, setMode, dark } = useTheme()
  const [version, setVersion] = useState<any>(null)

  useEffect(() => {
    api.get('/api/version').then((r) => setVersion(r.data)).catch(() => {})
    api.get('/api/auth/me').then((r) => setUser(r.data)).catch(() => nav('/login'))
    getProjects().then((ps) => {
      setProjects(ps)
      const pids = ps.filter((p) => p.can_access).map((p) => p.id)
      if (!pids.includes(projectId)) {
        const pid = pids[0] || 0
        setProjectId(pid)
        localStorage.setItem('projectId', String(pid))
      }
    }).catch(() => {})
  }, [])

  const changeProject = (v: number) => {
    setProjectId(v)
    localStorage.setItem('projectId', String(v))
  }

  const isSuper = user?.role === 'super_admin'
  const MENU = [
    { key: '/', icon: <DashboardOutlined />, label: t('nav.dashboard') },
    { key: '/data', icon: <DatabaseOutlined />, label: t('nav.data') },
    { key: '/models', icon: <ContainerOutlined />, label: t('nav.models') },
    { key: '/train', icon: <ExperimentOutlined />, label: t('nav.train') },
    { key: '/evals', icon: <BarChartOutlined />, label: t('nav.evals') },
    { key: '/quantize', icon: <ShrinkOutlined />, label: t('nav.quant') },
    { key: '/infer', icon: <CloudServerOutlined />, label: t('nav.infer') },
    { key: '/cluster', icon: <ClusterOutlined />, label: t('nav.cluster') },
  ]
  const menu = isSuper ? [...MENU, { key: '/admin', icon: <SettingOutlined />, label: t('nav.admin') }] : MENU

  // 语言选择器独立放 Header（不能嵌在 Dropdown 菜单项内：点击会冒泡触发菜单项关闭，下拉瞬间消失无法选中）
  const langSelect = (
    <Select size="small" style={{ width: 140 }} value={ti18n.language}
      options={LANGS.map((l) => ({ value: l.code, label: l.label }))}
      onChange={(v) => setLang(v)} suffixIcon={<GlobalOutlined />} />
  )

  const themeIcon = mode === 'dark' ? <MoonOutlined /> : mode === 'light' ? <SunOutlined /> : <DesktopOutlined />
  const themeToggle = (
    <Tooltip title={t('header.theme', { defaultValue: 'Theme' })}>
      <Select size="small" style={{ width: 130 }} value={mode}
        options={[
          { value: 'light', label: <><SunOutlined /> {t('header.themeLight', { defaultValue: 'Light' })}</> },
          { value: 'dark', label: <><MoonOutlined /> {t('header.themeDark', { defaultValue: 'Dark' })}</> },
          { value: 'system', label: <><DesktopOutlined /> {t('header.themeSystem', { defaultValue: 'System' })}</> },
        ]}
        onChange={(v) => setMode(v)} suffixIcon={themeIcon} />
    </Tooltip>
  )

  const userMenu = (
    <Dropdown menu={{
      items: [
        { key: 'logout', icon: <LogoutOutlined />, label: t('header.logout'), onClick: () => { localStorage.removeItem('token'); nav('/login') } },
      ],
    }}>
      <div style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8 }}>
        <Avatar size="small" icon={<RocketOutlined />} style={{ background: token.colorPrimary }} />
        <span>{user?.full_name || user?.username}</span>
      </div>
    </Dropdown>
  )

  const footer = (
    <Layout.Footer style={{ padding: '12px 24px', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 14, color: token.colorTextTertiary, fontSize: 12, background: token.colorBgLayout, borderTop: `1px solid ${token.colorBorderSecondary}` }}>
      <span>{version ? `VerveDeck v${version.version}` : 'VerveDeck'}</span>
      {version && <a href={version.license_url} target="_blank" rel="noreferrer" style={{ color: token.colorTextTertiary }}>{version.license}</a>}
      {version && <a href={version.github} target="_blank" rel="noreferrer" style={{ display: 'inline-flex', alignItems: 'center', gap: 4, color: token.colorTextTertiary }}><GithubOutlined /> GitHub</a>}
    </Layout.Footer>
  )

  return (
    <Layout style={{ minHeight: '100vh', background: token.colorBgLayout }}>
      <Sider width={210} theme={dark ? 'dark' : 'light'}>
        <div style={{ color: token.colorText, padding: '16px 20px', fontSize: 16, fontWeight: 700, display: 'flex', alignItems: 'center', gap: 8 }}>
          <img src="/logo.svg" alt="VerveDeck" style={{ width: 28, height: 28, borderRadius: 7 }} /> VerveDeck
        </div>
        <Menu theme={dark ? 'dark' : 'light'} mode="inline" selectedKeys={[loc.pathname]}
          items={menu.map((m) => ({ key: m.key, icon: m.icon, label: m.label }))}
          onClick={({ key }) => nav(key)} />
      </Sider>
      <Layout>
        <Header style={{ background: token.colorBgContainer, padding: '0 20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span style={{ color: token.colorTextSecondary, fontSize: 13 }}>{t('header.project')}</span>
            <Select size="small" style={{ width: 200 }} value={projectId || undefined}
              placeholder={t('header.selectProject')} onChange={changeProject}
              options={projects.filter((p) => p.can_access).map((p) => ({ value: p.id, label: p.name }))} />
            {user && <Tag color={user.role === 'super_admin' ? 'gold' : 'blue'}>{user.role}</Tag>}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            {themeToggle}
            {langSelect}
            {userMenu}
          </div>
        </Header>
        <Content style={{ margin: 16, padding: 16, background: token.colorBgContainer, borderRadius: 8, minHeight: 280 }}>
          {children}
        </Content>
        {footer}
      </Layout>
    </Layout>
  )
}

export default function App() {
  const [ready, setReady] = useState(false)
  const { t } = useTranslation()
  useEffect(() => { setReady(true) }, [])
  useEffect(() => { document.title = `VerveDeck · ${t('login.subtitle')}` }, [t])
  if (!ready) return null
  const hasToken = !!localStorage.getItem('token')
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={hasToken ? <Navigate to="/" replace /> : <Login />} />
        <Route path="/" element={hasToken ? <Shell><Dashboard /></Shell> : <Navigate to="/login" />} />
        <Route path="/data" element={hasToken ? <Shell><DataCenter /></Shell> : <Navigate to="/login" />} />
        <Route path="/models" element={hasToken ? <Shell><ModelRepo /></Shell> : <Navigate to="/login" />} />
        <Route path="/train" element={hasToken ? <Shell><TrainCenter /></Shell> : <Navigate to="/login" />} />
        <Route path="/evals" element={hasToken ? <Shell><EvalCenter /></Shell> : <Navigate to="/login" />} />
        <Route path="/quantize" element={hasToken ? <Shell><QuantCenter /></Shell> : <Navigate to="/login" />} />
        <Route path="/infer" element={hasToken ? <Shell><Inference /></Shell> : <Navigate to="/login" />} />
        <Route path="/cluster" element={hasToken ? <Shell><ClusterOps /></Shell> : <Navigate to="/login" />} />
        <Route path="/admin" element={hasToken ? <Shell><AdminPanel /></Shell> : <Navigate to="/login" />} />
      </Routes>
    </BrowserRouter>
  )
}
