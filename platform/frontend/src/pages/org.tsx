import React, { useEffect, useState } from 'react'
import { Button, Card, Form, Input, Modal, Select, Table, Tabs, message } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

/** 租户与项目空间（系统管理 tab 内容：租户 / 项目空间 两个子页，避免单页过长） */
export default function OrgSpace() {
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
                    title: t('admin.colMembers'), width: 240, render: (_: any, r: any) => (
                      <Select size="small" mode="multiple" style={{ width: 210 }} placeholder={t('admin.addMember')}
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
          ),
        },
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
