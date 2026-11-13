import React, { useEffect, useState, useCallback } from 'react'
import {
  Tabs, Table, Button, Space, Tag, Modal, Form, Input, Select, message, Popconfirm, Card,
} from 'antd'
import { PlusOutlined, CopyOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const ROLE_COLOR: Record<string, string> = { super_admin: 'gold', tenant_admin: 'purple', developer: 'blue', viewer: 'default' }

export default function AdminPanel() {
  const { t } = useTranslation()
  const [tab, setTab] = useState('users')
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('admin.title')}</h3>
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'users', label: t('admin.tabUsers'), children: <UsersTab /> },
        { key: 'org', label: t('admin.tabOrg'), children: <OrgTab /> },
        { key: 'keys', label: t('admin.tabKeys'), children: <KeysTab /> },
        { key: 'guard', label: t('admin.tabGuard'), children: <GuardTab /> },
        { key: 'audit', label: t('admin.tabAudit'), children: <AuditTab /> },
      ]} />
    </div>
  )
}

function UsersTab() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const load = () => api.get('/api/admin/users').then((r) => setList(r.data)).catch(() => {})
  useEffect(() => { load() }, [])
  return (
    <div>
      <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); form.setFieldsValue({ role: 'developer' }); setOpen(true) }} style={{ marginBottom: 12 }}>{t('admin.newUser')}</Button>
      <Table size="small" rowKey="id" dataSource={list} pagination={false}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('admin.colUsername'), dataIndex: 'username' },
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
                <Popconfirm title={t('admin.resetPwConfirm')} onConfirm={async () => { await api.put(`/api/admin/users/${r.id}`, { password: 'admin123' }); message.success(t('admin.resetOk')) }}>
                  <Button size="small">{t('admin.resetPw')}</Button>
                </Popconfirm>
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
    </div>
  )
}

function OrgTab() {
  const { t } = useTranslation()
  const [tenants, setTenants] = useState<any[]>([])
  const [projects, setProjects] = useState<any[]>([])
  const [users, setUsers] = useState<any[]>([])
  const [tOpen, setTOpen] = useState(false)
  const [pOpen, setPOpen] = useState(false)
  const [tForm] = Form.useForm()
  const [pForm] = Form.useForm()
  const load = () => {
    api.get('/api/admin/tenants').then((r) => setTenants(r.data)).catch(() => {})
    api.get('/api/admin/projects').then((r) => setProjects(r.data)).catch(() => {})
    api.get('/api/admin/users').then((r) => setUsers(r.data)).catch(() => {})
  }
  useEffect(() => { load() }, [])
  return (
    <div>
      <Card size="small" title={t('admin.tenants')} style={{ marginBottom: 12 }}>
        <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => { tForm.resetFields(); setTOpen(true) }}>{t('admin.newTenant')}</Button>
        <Table size="small" rowKey="id" dataSource={tenants} pagination={false} style={{ marginTop: 8 }}
          columns={[{ title: t('common.id'), dataIndex: 'id', width: 60 }, { title: t('common.name'), dataIndex: 'name' }]} />
      </Card>
      <Card size="small" title={t('admin.projects')}>
        <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => { pForm.resetFields(); setPOpen(true) }}>{t('admin.newProject')}</Button>
        <Table size="small" rowKey="id" dataSource={projects} pagination={false} style={{ marginTop: 8 }}
          columns={[
            { title: t('common.id'), dataIndex: 'id', width: 60 },
            { title: t('common.name'), dataIndex: 'name' },
            { title: t('admin.colTenant'), dataIndex: 'tenant_id', width: 70 },
            { title: t('common.desc'), dataIndex: 'description', ellipsis: true },
            {
              title: t('admin.colMembers'), width: 220, render: (_: any, r: any) => (
                <Select size="small" mode="multiple" style={{ width: 190 }} placeholder={t('admin.addMember')}
                  options={users.map((u: any) => ({ value: u.id, label: u.username }))}
                  onChange={async (vals) => {
                    const all = users.map((u) => u.id)
                    const removed = all.filter((id) => !vals.includes(id))
                    for (const id of removed) await api.delete(`/api/admin/projects/${r.id}/members/${id}`).catch(() => {})
                    for (const id of vals) await api.put(`/api/admin/projects/${r.id}/members`, { user_id: id, role: 'developer' }).catch(() => {})
                  }} />
              ),
            },
          ]} />
      </Card>
      <Modal title={t('admin.newTenant')} open={tOpen} onCancel={() => setTOpen(false)} onOk={async () => { await api.post('/api/admin/tenants', await tForm.validateFields()); message.success(t('admin.created')); setTOpen(false); load() }}>
        <Form form={tForm} layout="vertical"><Form.Item name="name" label={t('admin.tenantName')} rules={[{ required: true }]}><Input /></Form.Item></Form>
      </Modal>
      <Modal title={t('admin.projectModal')} open={pOpen} onCancel={() => setPOpen(false)} onOk={async () => { await api.post('/api/admin/projects', await pForm.validateFields()); message.success(t('admin.created')); setPOpen(false); load() }}>
        <Form form={pForm} layout="vertical">
          <Form.Item name="tenant_id" label={t('admin.ownerTenant')} rules={[{ required: true }]}><Select options={tenants.map((t) => ({ value: t.id, label: t.name }))} /></Form.Item>
          <Form.Item name="name" label={t('admin.projectName')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label={t('common.desc')}><Input /></Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

function KeysTab() {
  const { t } = useTranslation()
  const [projects, setProjects] = useState<any[]>([])
  const [keys, setKeys] = useState<any[]>([])
  const [pid, setPid] = useState<number>(0)
  const [newKey, setNewKey] = useState('')
  const load = () => {
    api.get('/api/admin/projects').then((r) => {
      setProjects(r.data)
      if (r.data.length && !r.data.find((p) => p.id === pid)) setPid(r.data[0].id)
    }).catch(() => {})
  }
  const loadKeys = () => pid && api.get(`/api/admin/projects/${pid}/api-keys`).then((r) => setKeys(r.data)).catch(() => {})
  useEffect(() => { load() }, [])
  useEffect(() => { loadKeys() }, [pid])
  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Select value={pid || undefined} style={{ width: 240 }} placeholder={t('admin.selectProject')}
          options={projects.map((p) => ({ value: p.id, label: p.name }))} onChange={setPid} />
        <Button type="primary" size="small" icon={<PlusOutlined />} onClick={async () => {
          const r = await api.post(`/api/admin/projects/${pid}/api-keys`, { name: 'default' })
          setNewKey(r.data.key)
        }}>{t('admin.createKey')}</Button>
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
          { title: t('common.status'), dataIndex: 'is_active', width: 80, render: (v: boolean) => v ? <Tag color="success">{t('common.enabled')}</Tag> : <Tag>{t('common.disabled')}</Tag> },
          {
            title: t('common.actions'), width: 90, render: (_: any, r: any) => (
              <Popconfirm title={t('admin.disableKey')} onConfirm={() => api.delete(`/api/admin/projects/${pid}/api-keys/${r.id}`).then(loadKeys)}>
                <Button size="small" danger>{t('admin.disable')}</Button>
              </Popconfirm>
            ),
          },
        ]} />
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

function AuditTab() {
  const { t, i18n: ti18n } = useTranslation()
  const [data, setData] = useState<any>({ items: [], total: 0 })
  const [page, setPage] = useState(1)
  const [user, setUser] = useState('')
  const load = useCallback(() => {
    api.get('/api/admin/audit', { params: { limit: 20, offset: (page - 1) * 20, username: user } })
      .then((r) => setData(r.data)).catch(() => {})
  }, [page, user])
  useEffect(() => { load() }, [load])
  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Input placeholder={t('admin.auditFilter')} value={user} onChange={(e) => { setUser(e.target.value); setPage(1) }} style={{ width: 200 }} />
      </Space>
      <Table size="small" rowKey="id" dataSource={data.items}
        pagination={{ current: page, total: data.total, pageSize: 20, onChange: setPage }}
        columns={[
          { title: t('admin.colTime'), dataIndex: 'ts', width: 170, render: (ts: number) => new Date(ts * 1000).toLocaleString(ti18n.language) },
          { title: t('admin.colUser'), dataIndex: 'username', width: 110 },
          { title: t('admin.colMethod'), dataIndex: 'method', width: 70 },
          { title: t('admin.colPath'), dataIndex: 'path', ellipsis: true },
          { title: t('admin.colIp'), dataIndex: 'ip', width: 130 },
          { title: t('admin.colCode'), dataIndex: 'status_code', width: 80 },
        ]} />
    </div>
  )
}
