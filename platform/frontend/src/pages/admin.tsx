import React, { useEffect, useState } from 'react'
import {
  Tabs, Table, Button, Space, Tag, Modal, Form, Input, Select, message, Popconfirm, Card,
} from 'antd'
import { PlusOutlined, CopyOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useTheme } from '../theme'
import { LANGS, setLang } from '../i18n'
import OrgSpace from './org'
import StorageTab from './storage'

const ROLE_COLOR: Record<string, string> = { super_admin: 'gold', tenant_admin: 'purple', developer: 'blue', viewer: 'default' }

export default function AdminPanel() {
  const { t } = useTranslation()
  const [tab, setTab] = useState('users')
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('admin.title')}</h3>
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'users', label: t('admin.tabUsers'), children: <UsersTab /> },
        { key: 'org', label: t('admin.tabOrg'), children: <OrgSpace /> },
        { key: 'keys', label: t('admin.tabKeys'), children: <KeysTab /> },
        { key: 'guard', label: t('admin.tabGuard'), children: <GuardTab /> },
        { key: 'storage', label: t('storage.title'), children: <StorageTab /> },
        { key: 'settings', label: t('admin.tabSettings'), children: <SettingsTab /> },
      ]} />
    </div>
  )
}

function UsersTab() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [resetUser, setResetUser] = useState<any>(null)
  const [resetForm] = Form.useForm()
  const load = () => api.get('/api/admin/users').then((r) => setList(r.data)).catch(() => {})
  useEffect(() => { load() }, [])
  return (
    <div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); form.setFieldsValue({ role: 'developer' }); setOpen(true) }} style={{ marginBottom: 12 }}>{t('admin.newUser')}</Button>
      <Table size="small" rowKey="id" dataSource={list} pagination={false}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          {
            title: t('admin.colUsername'), dataIndex: 'username', render: (v: string, r: any) => (
              <Space size={4}>{v}{r.must_change_password && <Tag color="orange">{t('admin.mustChange')}</Tag>}</Space>
            ),
          },
          { title: t('admin.colName'), dataIndex: 'full_name' },
          { title: t('admin.colRole'), dataIndex: 'role', width: 140, render: (v: string) => <Tag color={ROLE_COLOR[v]}>{v}</Tag> },
          { title: t('admin.colTenant'), dataIndex: 'tenant_id', width: 70 },
          { title: t('common.status'), dataIndex: 'is_active', width: 80, render: (v: boolean) => v ? <Tag color="success">{t('common.enabled')}</Tag> : <Tag>{t('common.disabled')}</Tag> },
          {
            title: t('common.actions'), width: 200, render: (_: any, r: any) => (
              <Space size={4}>
                <Select size="small" value={r.role} style={{ width: 130 }}
                  options={Object.keys(ROLE_COLOR).map((x) => ({ value: x, label: x }))}
                  onChange={async (v) => { await api.put(`/api/admin/users/${r.id}`, { role: v }); load() }} />
                <Button size="small" onClick={() => { resetForm.resetFields(); setResetUser(r) }}>{t('admin.resetPw')}</Button>
              </Space>
            ),
          },
        ]} />
      <Modal title={t('admin.newUser')} open={open} onCancel={() => setOpen(false)}
        onOk={async () => {
          const v = await form.validateFields()
          await api.post('/api/admin/users', v)
          message.success(t('admin.created')); setOpen(false); load()
        }}>
        <Form form={form} layout="vertical">
          <Form.Item name="username" label={t('admin.colUsername')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="password" label={t('admin.initPw')} rules={[{ required: true, min: 6 }]}><Input.Password /></Form.Item>
          <Form.Item name="full_name" label={t('admin.colName')}><Input /></Form.Item>
          <Form.Item name="role" label={t('admin.role')}><Select options={Object.keys(ROLE_COLOR).map((x) => ({ value: x, label: x }))} /></Form.Item>
        </Form>
      </Modal>
      <Modal title={`${t('admin.resetPwModal')}: ${resetUser?.username || ''}`} open={!!resetUser}
        onCancel={() => setResetUser(null)}
        onOk={async () => {
          const v = await resetForm.validateFields()
          await api.put(`/api/admin/users/${resetUser.id}`, { password: v.password })
          message.success(t('admin.resetOk')); setResetUser(null); load()
        }}>
        <p style={{ color: '#888', fontSize: 12, marginTop: 0 }}>{t('admin.resetPwHint')}</p>
        <Form form={resetForm} layout="vertical">
          <Form.Item name="password" label={t('admin.resetPwLabel')} rules={[{ required: true, min: 6 }]}><Input.Password autoComplete="new-password" /></Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

function KeysTab() {
  const { t } = useTranslation()
  const [projects, setProjects] = useState<any[]>([])
  const [keys, setKeys] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [pid, setPid] = useState<number>(0)
  const [newKey, setNewKey] = useState('')
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const load = () => {
    api.get('/api/admin/projects').then((r) => {
      setProjects(r.data)
      if (r.data.length && !r.data.find((p) => p.id === pid)) setPid(r.data[0].id)
    }).catch(() => {})
  }
  const loadKeys = () => {
    if (!pid) return
    api.get(`/api/admin/projects/${pid}/api-keys`).then((r) => setKeys(r.data)).catch(() => {})
    api.get('/api/models', { params: { project_id: pid } }).then((r) => setModels(r.data || [])).catch(() => {})
  }
  useEffect(() => { load() }, [])
  useEffect(() => { loadKeys() }, [pid])
  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Select value={pid || undefined} style={{ width: 240 }} placeholder={t('admin.selectProject')}
          options={projects.map((p) => ({ value: p.id, label: p.name }))} onChange={setPid} />
        <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setOpen(true) }}>{t('admin.createKey')}</Button>
      </Space>
      {newKey && (
        <Card size="small" style={{ marginBottom: 12, borderColor: '#fa8c16' }}>
          <Space>
            <code style={{ fontSize: 13 }}>{newKey}</code>
            <Button size="small" icon={<CopyOutlined />} onClick={() => navigator.clipboard?.writeText(newKey)}>{t('admin.copy')}</Button>
            <Tag color="orange">{t('admin.showOnce')}</Tag>
          </Space>
        </Card>
      )}
      <Table size="small" rowKey="id" dataSource={keys} pagination={false}
        columns={[
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('admin.colPrefix'), dataIndex: 'key_prefix', render: (v: string) => <code>{v}…</code> },
          {
            title: t('admin.keyModels'), dataIndex: 'model_ids', width: 160,
            render: (ids: number[]) => (!ids || !ids.length)
              ? <Tag>{t('admin.keyScopeAll')}</Tag>
              : <Tag color="blue">{t('admin.keyScopeN', { n: ids.length })}</Tag>,
          },
          { title: t('common.status'), dataIndex: 'is_active', width: 80, render: (v: boolean) => v ? <Tag color="success">{t('common.enabled')}</Tag> : <Tag>{t('common.disabled')}</Tag> },
          {
            title: t('common.actions'), width: 90, render: (_: any, r: any) => (
              <Popconfirm title={t('admin.disableKey')} onConfirm={() => api.delete(`/api/admin/projects/${pid}/api-keys/${r.id}`).then(loadKeys)}>
                <Button size="small" danger>{t('admin.disable')}</Button>
              </Popconfirm>
            ),
          },
        ]} />
      <Modal title={t('admin.createKey')} open={open} onCancel={() => setOpen(false)}
        onOk={async () => {
          const v = await form.validateFields()
          const r = await api.post(`/api/admin/projects/${pid}/api-keys`, v)
          setNewKey(r.data.key); setOpen(false); loadKeys()
        }}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('common.name')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="model_ids" label={t('admin.keyModels')} extra={t('admin.keyModelsHint')}>
            <Select mode="multiple" allowClear optionFilterProp="label" placeholder={t('admin.keyScopeAll')}
              options={models.map((m: any) => ({ value: m.id, label: m.name }))} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

function GuardTab() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [form] = Form.useForm()
  const load = () => api.get('/api/admin/guard-rules').then((r) => setList(r.data)).catch(() => {})
  useEffect(() => { load() }, [])
  return (
    <div>
      <Card size="small" title={t('admin.guardTitle')} style={{ marginBottom: 12 }}>
        <Form form={form} layout="inline" onFinish={async (v) => { await api.post('/api/admin/guard-rules', v); message.success(t('admin.created')); form.resetFields(); load() }}>
          <Form.Item name="pattern" rules={[{ required: true }]}><Input placeholder={t('admin.patternPh')} style={{ width: 240 }} /></Form.Item>
          <Form.Item name="scope"><Select style={{ width: 130 }} options={[{ value: 'both', label: t('admin.scopeBoth') }, { value: 'input', label: t('admin.scopeInput') }, { value: 'output', label: t('admin.scopeOutput') }]} /></Form.Item>
          <Form.Item name="description"><Input placeholder={t('admin.descPh')} style={{ width: 200 }} /></Form.Item>
          <Button type="primary" htmlType="submit">{t('admin.add')}</Button>
        </Form>
      </Card>
      <Table size="small" rowKey="id" dataSource={list} pagination={false}
        columns={[
          { title: t('admin.colPattern'), dataIndex: 'pattern' },
          { title: t('admin.colScope'), dataIndex: 'scope', width: 100 },
          { title: t('common.desc'), dataIndex: 'description', ellipsis: true },
          { title: t('admin.colEnabled'), dataIndex: 'enabled', width: 70, render: (v: boolean) => v ? <Tag color="success">{t('common.yes')}</Tag> : <Tag>{t('common.no')}</Tag> },
          { title: t('common.actions'), width: 80, render: (_: any, r: any) => <Popconfirm title={t('admin.del')} onConfirm={() => api.delete(`/api/admin/guard-rules/${r.id}`).then(load)}><Button size="small" danger>{t('common.delete')}</Button></Popconfirm> },
        ]} />
    </div>
  )
}

/** 平台设置：主题 + 语言（原 Header 右上角入口迁至此） */
function SettingsTab() {
  const { t, i18n: ti18n } = useTranslation()
  const { mode, setMode } = useTheme()
  return (
    <Card size="small" title={t('admin.tabSettings')} style={{ maxWidth: 480 }}>
      <Form layout="vertical">
        <Form.Item label={t('header.theme')}>
          <Select value={mode} style={{ width: 200 }}
            options={[
              { value: 'light', label: t('header.themeLight') },
              { value: 'dark', label: t('header.themeDark') },
              { value: 'system', label: t('header.themeSystem') },
            ]}
            onChange={(v) => setMode(v)} />
        </Form.Item>
        <Form.Item label={t('admin.langLabel')}>
          <Select value={ti18n.language} style={{ width: 200 }}
            options={LANGS.map((l) => ({ value: l.code, label: l.label }))}
            onChange={(v) => setLang(v)} />
        </Form.Item>
      </Form>
    </Card>
  )
}
