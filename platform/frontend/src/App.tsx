import React, { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, useNavigate, useLocation, Navigate } from 'react-router-dom'
import { Layout, Menu, Dropdown, Avatar, Select, Tag, theme as antTheme,
  Modal, Form, Input, Button, Card, message } from 'antd'
import { useTranslation } from 'react-i18next'
import {
  DashboardOutlined, DatabaseOutlined, ContainerOutlined, ExperimentOutlined,
  BarChartOutlined, CloudServerOutlined, ClusterOutlined, SettingOutlined,
  LogoutOutlined, RocketOutlined, ShrinkOutlined,
  GithubOutlined, KeyOutlined,
  ApartmentOutlined, HddOutlined, AppstoreOutlined,
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
import LogsPage from './pages/logs'
import AdminPanel, { AlertsPage } from './pages/admin'
import OrgPage from './pages/org'
import StoragePage from './pages/storage'
import ImagesPage from './pages/images'
import { api, getProjects } from './api'

const { Sider, Header, Content } = Layout

/** 改密表单（复用：用户菜单弹框 + 首登强制改密页） */
function ChangePasswordPanel({ onDone, submitLabel }: { onDone: () => void; submitLabel: string }) {
  const { t } = useTranslation()
  const [form] = Form.useForm()
  const [busy, setBusy] = useState(false)
  return (
    <Form form={form} layout="vertical" style={{ maxWidth: 380 }}
      onFinish={async (v: any) => {
        if (v.new_password !== v.confirm) { message.error(t('auth.pwMismatch')); return }
        setBusy(true)
        try {
          await api.put('/api/auth/password', { old_password: v.old_password, new_password: v.new_password })
          message.success(t('auth.pwChanged'))
          onDone()
        } catch (e: any) {
          message.error(e.response?.data?.detail || t('api.requestFailed'))
        } finally {
          setBusy(false)
        }
      }}>
      <Form.Item name="old_password" label={t('auth.oldPw')} rules={[{ required: true }]}>
        <Input.Password autoComplete="current-password" />
      </Form.Item>
      <Form.Item name="new_password" label={t('auth.newPw')} rules={[{ required: true, min: 6 }]}
        extra={t('auth.newPwHint')}>
        <Input.Password autoComplete="new-password" />
      </Form.Item>
      <Form.Item name="confirm" label={t('auth.confirmPw')} rules={[{ required: true }]}>
        <Input.Password autoComplete="new-password" />
      </Form.Item>
      <Button type="primary" htmlType="submit" block loading={busy}>{submitLabel}</Button>
    </Form>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  const nav = useNavigate()
  const loc = useLocation()
  const { t } = useTranslation()
  const [user, setUser] = useState<any>(null)
  const [projects, setProjects] = useState<any[]>([])
  const [projectId, setProjectId] = useState<number>(Number(localStorage.getItem('projectId') || 0))
  const { token } = antTheme.useToken()
  const { dark } = useTheme()
  const [version, setVersion] = useState<any>(null)
  const [pwOpen, setPwOpen] = useState(false)

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
    // 存储管理 / 镜像管理 置于集群运维上方（仅超管可见）
    ...(isSuper ? [
      { key: '/storage', icon: <HddOutlined />, label: t('nav.storage') },
      { key: '/images', icon: <AppstoreOutlined />, label: t('nav.images') },
    ] : []),
    {
      key: '/cluster', icon: <ClusterOutlined />, label: t('nav.cluster'),
      children: [
        { key: '/cluster/nodes', label: t('cluster.tabNodes') },
        { key: '/cluster/gpus', label: t('cluster.tabGpus') },
        { key: '/cluster/monitor', label: t('cluster.tabMonitor') },
        // 告警中心 + 日志中心统一归入集群运维（仅超管可见，API 侧仍有角色校验）
        ...(isSuper ? [
          { key: '/cluster/alerts', label: t('admin.tabAlerts') },
          { key: '/cluster/logs', label: t('nav.logs') },
        ] : []),
      ],
    },
    ...(isSuper ? [
      { key: '/org', icon: <ApartmentOutlined />, label: t('nav.org') },
      { key: '/admin', icon: <SettingOutlined />, label: t('nav.admin') },
    ] : []),
  ]

  const forcePw = !!user?.must_change_password

  const userMenu = (
    <Dropdown menu={{
      items: [
        { key: 'changepw', icon: <KeyOutlined />, label: t('header.changePassword'), onClick: () => setPwOpen(true) },
        { type: 'divider' },
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
          defaultOpenKeys={['/cluster']}
          items={MENU}
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
            {userMenu}
          </div>
        </Header>
        {/* key=projectId：切换项目时整体重挂载页面，数据随项目刷新（无需手动刷新页面） */}
        <Content key={projectId} style={{ margin: 16, padding: 16, background: token.colorBgContainer, borderRadius: 8, minHeight: 280 }}>
          {forcePw ? (
            <div style={{ display: 'flex', justifyContent: 'center', paddingTop: 48 }}>
              <Card title={<><KeyOutlined /> {t('auth.forceTitle')}</>} style={{ width: 460, textAlign: 'left' }}>
                <p style={{ color: token.colorTextSecondary, fontSize: 13, marginTop: 0 }}>{t('auth.forceHint')}</p>
                <ChangePasswordPanel submitLabel={t('auth.changePwTitle')}
                  onDone={() => setUser({ ...user, must_change_password: false })} />
              </Card>
            </div>
          ) : children}
        </Content>
        {!forcePw && (
          <Modal title={t('auth.changePwTitle')} open={pwOpen} onCancel={() => setPwOpen(false)} footer={null} destroyOnClose>
            <ChangePasswordPanel submitLabel={t('auth.changePwTitle')}
              onDone={() => { setPwOpen(false); setUser({ ...user, must_change_password: false }) }} />
          </Modal>
        )}
        {footer}
      </Layout>
    </Layout>
  )
}

/** 路由表放在 Router 内部组件里：订阅 location 变化后重渲染，
 *  保证 hasToken 始终读取最新 localStorage（修复：登录成功后不立即跳转、需手动刷新的问题） */
function AppRoutes() {
  useLocation()
  const hasToken = !!localStorage.getItem('token')
  return (
    <Routes>
      <Route path="/login" element={hasToken ? <Navigate to="/" replace /> : <Login />} />
      <Route path="/" element={hasToken ? <Shell><Dashboard /></Shell> : <Navigate to="/login" />} />
      <Route path="/data" element={hasToken ? <Shell><DataCenter /></Shell> : <Navigate to="/login" />} />
      <Route path="/models" element={hasToken ? <Shell><ModelRepo /></Shell> : <Navigate to="/login" />} />
      <Route path="/train" element={hasToken ? <Shell><TrainCenter /></Shell> : <Navigate to="/login" />} />
      <Route path="/evals" element={hasToken ? <Shell><EvalCenter /></Shell> : <Navigate to="/login" />} />
      <Route path="/quantize" element={hasToken ? <Shell><QuantCenter /></Shell> : <Navigate to="/login" />} />
      <Route path="/infer" element={hasToken ? <Shell><Inference /></Shell> : <Navigate to="/login" />} />
      <Route path="/cluster" element={hasToken ? <Shell><Navigate to="/cluster/nodes" replace /></Shell> : <Navigate to="/login" />} />
      <Route path="/cluster/nodes" element={hasToken ? <Shell><ClusterOps tab="nodes" /></Shell> : <Navigate to="/login" />} />
      <Route path="/cluster/gpus" element={hasToken ? <Shell><ClusterOps tab="gpus" /></Shell> : <Navigate to="/login" />} />
      <Route path="/cluster/monitor" element={hasToken ? <Shell><ClusterOps tab="monitor" /></Shell> : <Navigate to="/login" />} />
      <Route path="/cluster/alerts" element={hasToken ? <Shell><AlertsPage /></Shell> : <Navigate to="/login" />} />
      <Route path="/cluster/logs" element={hasToken ? <Shell><LogsPage /></Shell> : <Navigate to="/login" />} />
      <Route path="/org" element={hasToken ? <Shell><OrgPage /></Shell> : <Navigate to="/login" />} />
      <Route path="/storage" element={hasToken ? <Shell><StoragePage /></Shell> : <Navigate to="/login" />} />
      <Route path="/images" element={hasToken ? <Shell><ImagesPage /></Shell> : <Navigate to="/login" />} />
      <Route path="/logs" element={<Navigate to="/cluster/logs" replace />} />
      <Route path="/admin" element={hasToken ? <Shell><AdminPanel /></Shell> : <Navigate to="/login" />} />
    </Routes>
  )
}

export default function App() {
  const [ready, setReady] = useState(false)
  const { t } = useTranslation()
  useEffect(() => { setReady(true) }, [])
  useEffect(() => { document.title = `VerveDeck · ${t('login.subtitle')}` }, [t])
  if (!ready) return null
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  )
}
