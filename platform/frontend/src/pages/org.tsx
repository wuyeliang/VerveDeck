import React, { useEffect, useState } from 'react'
import { Button, Card, Form, Input, Modal, Select, Space, Table, Tabs, Tag, message } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const ROLE_COLOR: Record<string, string> = { super_admin: 'gold', tenant_admin: 'purple', developer: 'blue', viewer: 'default' }

/** 用户中心（一级菜单 /org，原「租户与项目」：租户 / 项目空间 两个子页，避免单页过长） */
export default function OrgPage() {
  const { t } = useTranslation()
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('nav.org')}</h3>
      <OrgSpace />
    </div>
  )
}

function OrgSpace() {
  const { t } = useTranslation()
  const [sub, setSub] = useState('tenants')
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
      <Tabs activeKey={sub} onChange={setSub} items={[
        {
          key: 'tenants', label: t('admin.tenants'),
          children: (
            <Card size="small">
              <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => { tForm.resetFields(); setTOpen(true) }}>{t('admin.newTenant')}</Button>
              <Table size="small" rowKey="id" dataSource={tenants} pagination={false} style={{ marginTop: 8 }}
                columns={[
                  { title: t('common.id'), dataIndex: 'id', width: 60 },
                  { title: t('common.name'), dataIndex: 'name' },
                  {
                    title: t('admin.projects'), render: (_: any, r: any) => (
                      <span style={{ color: '#888', fontSize: 12 }}>
                        {projects.filter((p: any) => p.tenant_id === r.id).map((p: any) => p.name).join('、') || '-'}
                      </span>
                    ),
                  },
                ]} />
            </Card>
          ),
        },
        {
          key: 'projects', label: t('admin.projects'),
          children: (
            <Card size="small">
              <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => { pForm.resetFields(); setPOpen(true) }}>{t('admin.newProject')}</Button>
              <Table size="small" rowKey="id" dataSource={projects} pagination={false} style={{ marginTop: 8 }}
                columns={[
                  { title: t('common.id'), dataIndex: 'id', width: 60 },
                  { title: t('common.name'), dataIndex: 'name' },
                  {
                    title: t('admin.colTenant'), dataIndex: 'tenant_id', width: 90,
                    render: (v: number) => tenants.find((x) => x.id === v)?.name || v,
                  },
                  { title: t('common.desc'), dataIndex: 'description', ellipsis: true },
                  {
                    title: t('admin.colMembers'), width: 260, render: (_: any, r: any) => (
                      <Select size="small" mode="multiple" style={{ width: 230 }} placeholder={t('admin.addMember')}
                        value={(r.members || []).slice()}
                        options={users.map((u: any) => ({ value: u.id, label: u.username }))}
                        onChange={(vals: number[]) => {
                          // 只对真实 diff 动手：新增走 put（幂等），移除先弹确认再 delete
                          const prev: number[] = r.members || []
                          const removed = prev.filter((id) => !vals.includes(id))
                          const added = vals.filter((id) => !prev.includes(id))
                          const apply = async () => {
                            for (const id of removed) await api.delete(`/api/admin/projects/${r.id}/members/${id}`).catch(() => {})
                            for (const id of added) await api.put(`/api/admin/projects/${r.id}/members`, { user_id: id, role: 'developer' }).catch(() => {})
                            load()
                          }
                          if (removed.length) {
                            const names = removed.map((id) => users.find((u: any) => u.id === id)?.username || `#${id}`).join('、')
                            Modal.confirm({
                              title: t('admin.rmMemberTitle'),
                              content: t('admin.rmMemberConfirm', { names }),
                              okText: t('common.confirm'),
                              cancelText: t('common.cancel'),
                              okButtonProps: { danger: true },
                              onOk: apply,
                            })
                          } else apply()
                        }} />
                    ),
                  },
                ]} />
            </Card>
          ),
        },
        { key: 'users', label: t('admin.tabUsers'), children: <UsersTab /> },
      ]} />
      <Modal title={t('admin.newTenant')} open={tOpen} onCancel={() => setTOpen(false)} onOk={async () => { await api.post('/api/admin/tenants', await tForm.validateFields()); message.success(t('admin.created')); setTOpen(false); load() }}>
        <Form form={tForm} layout="vertical"><Form.Item name="name" label={t('admin.tenantName')} rules={[{ required: true }]}><Input /></Form.Item></Form>
      </Modal>
      <Modal title={t('admin.projectModal')} open={pOpen} onCancel={() => setPOpen(false)} onOk={async () => { await api.post('/api/admin/projects', await pForm.validateFields()); message.success(t('admin.created')); setPOpen(false); load() }}>
        <Form form={pForm} layout="vertical">
          <Form.Item name="tenant_id" label={t('admin.ownerTenant')} rules={[{ required: true }]}><Select options={tenants.map((x) => ({ value: x.id, label: x.name }))} /></Form.Item>
          <Form.Item name="name" label={t('admin.projectName')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="description" label={t('common.desc')}><Input /></Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

/** 用户与角色（自系统管理迁入：用户 CRUD / 角色切换 / 重置密码） */
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
                  onChange={(v) => {
                    if (v === r.role) return
                    // 角色变更=权限变更，确认后再提交
                    Modal.confirm({
                      title: t('admin.roleChangeConfirm', { user: r.username, role: v }),
                      okText: t('common.confirm'),
                      cancelText: t('common.cancel'),
                      onOk: async () => { await api.put(`/api/admin/users/${r.id}`, { role: v }); load() },
                    })
                  }} />
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
