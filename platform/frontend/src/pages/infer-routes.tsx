import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select,
  message, Popconfirm, Switch, Tooltip,
} from 'antd'
import {
  PlusOutlined, CopyOutlined, EditOutlined, DeleteOutlined,
  MinusCircleOutlined, ExclamationCircleOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const SYNC_ST: Record<string, string> = { pending: 'processing', synced: 'success', error: 'error' }

/** 模型路由：服务名 → 多部署目标（权重 + 容灾），数据面走 Higress 网关 */
export default function InferRoutes() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [deploys, setDeploys] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const [editId, setEditId] = useState<number | null>(null)
  const [form] = Form.useForm()

  const load = useCallback(() => {
    api.get('/api/inference/routes', { params: projectId ? { project_id: projectId } : {} })
      .then((r) => setList(r.data || [])).catch(() => {})
    api.get('/api/inference/deployments')
      .then((r) => setDeploys(r.data || [])).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const x = setInterval(load, 5000); return () => clearInterval(x) }, [load])

  const copy = (text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => message.success(t('infer.copied')),
      () => message.error(t('infer.copy')),
    )
  }

  const openCreate = () => {
    form.resetFields()
    form.setFieldsValue({ name: '', description: '', targets: [{ deployment_id: undefined, weight: 100, is_fallback: false }] })
    setEditId(null)
    setOpen(true)
  }

  const openEdit = (r: any) => {
    form.resetFields()
    form.setFieldsValue({
      name: r.name, description: r.description || '',
      targets: r.targets.length
        ? r.targets.map((x: any) => ({ deployment_id: x.deployment_id, weight: x.weight, is_fallback: x.is_fallback }))
        : [{ deployment_id: undefined, weight: 100, is_fallback: false }],
    })
    setEditId(r.id)
    setOpen(true)
  }

  const submit = async () => {
    const v = await form.validateFields()
    const targets = (v.targets || []).map((x: any) => ({
      deployment_id: x.deployment_id, weight: x.weight ?? 100, is_fallback: !!x.is_fallback,
    }))
    if (editId) {
      // 路由名即对外 API 前缀（/v1/<name>/），不可改（换名请删除重建，与后端注册表命名口径一致）
      await api.put(`/api/inference/routes/${editId}`, { description: v.description || '', targets })
    } else {
      await api.post('/api/inference/routes', {
        project_id: projectId, name: v.name, description: v.description || '', targets,
      })
    }
    message.success(t('routes.saved'))
    setOpen(false)
    load()
  }

  const setEnabled = (r: any, checked: boolean) => {
    api.put(`/api/inference/routes/${r.id}`, { enabled: checked })
      .then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  const del = (id: number) => {
    api.delete(`/api/inference/routes/${id}`).then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  const depName = (id: number) => deploys.find((d: any) => d.id === id)?.name || `#${id}`
  const depStatus = (id: number) => deploys.find((d: any) => d.id === id)?.status || 'deleted'

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('routes.title')}</h3>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t('routes.new')}</Button>
      </Space>
      <p style={{ color: '#888', fontSize: 12, marginTop: 0 }}>{t('routes.hint')}</p>
      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }} scroll={{ x: 1180 }}
        columns={[
          {
            title: t('common.name'), dataIndex: 'name', width: 200, render: (v: string, r: any) => (
              <Space size={4}>
                <span style={{ fontWeight: 600 }}>{v}</span>
                <code style={{ fontSize: 11, color: '#888' }}>{r.endpoint}</code>
                <Button size="small" type="text" icon={<CopyOutlined />}
                  onClick={() => copy(`${location.origin}/v1/${r.name}`)} />
              </Space>
            ),
          },
          {
            title: t('routes.targets'), width: 320, render: (_: any, r: any) => (
              <Space size={[4, 4]} wrap>
                {(r.targets || []).length === 0 && <span style={{ color: '#bbb' }}>{t('routes.noTargets')}</span>}
                {(r.targets || []).map((x: any) => (
                  <Tag key={x.id} color={x.is_fallback ? 'gold' : 'blue'}>
                    {x.deployment_name} {x.weight}
                    {x.is_fallback ? ` · ${t('routes.fallback')}` : ''}
                    {x.deployment_status !== 'running' && ` (${x.deployment_status})`}
                  </Tag>
                ))}
              </Space>
            ),
          },
          {
            title: t('common.status'), width: 210, render: (_: any, r: any) => (
              <Space size={6} wrap>
                <Switch size="small" checked={r.enabled} onChange={(c) => setEnabled(r, c)}
                  checkedChildren={t('common.enabled')} unCheckedChildren={t('common.disabled')} />
                <Tag color={SYNC_ST[r.sync_status] || 'default'}>
                  {r.enabled ? (r.sync_status === 'pending' ? t('routes.pending') : r.sync_status === 'error' ? t('routes.syncErr') : t('routes.synced'))
                    : t('common.disabled')}
                </Tag>
                {r.sync_message && (
                  <Tooltip title={r.sync_message}>
                    <ExclamationCircleOutlined style={{ color: r.sync_status === 'error' ? '#cf1322' : '#8c8c8c' }} />
                  </Tooltip>
                )}
              </Space>
            ),
          },
          { title: t('common.desc'), dataIndex: 'description', ellipsis: true, render: (v: string) => v || <span style={{ color: '#bbb' }}>-</span> },
          {
            title: t('common.actions'), width: 130, render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>{t('common.edit')}</Button>
                <Popconfirm title={t('routes.deleteConfirm')} okButtonProps={{ danger: true }} onConfirm={() => del(r.id)}>
                  <Button size="small" danger icon={<DeleteOutlined />}>{t('common.delete')}</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]} />

      <Modal title={editId ? t('routes.editTitle') : t('routes.new')} open={open} onCancel={() => setOpen(false)}
        width={720} onOk={submit} okText={t('common.confirm')}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('routes.name')}
            tooltip={editId ? t('routes.nameLocked') : undefined}
            rules={[{ required: true, pattern: /^[a-z0-9][a-z0-9-]*$/, message: t('routes.nameRule') }, { max: 128 }]}>
            <Input placeholder={t('routes.namePh')} showCount maxLength={128}
              disabled={!!editId} />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(a, b) => a.name !== b.name}>
            {() => {
              const n = (form.getFieldValue('name') || '').toLowerCase()
              const clash = deploys.find((d: any) => d.name === n && n)
              return clash && !editId
                ? <p style={{ color: '#cf1322', fontSize: 12, marginTop: -12, marginBottom: 8 }}>
                  {t('routes.nameClash')}
                </p>
                : null
            }}
          </Form.Item>
          <Form.Item name="description" label={t('common.desc')}>
            <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
          </Form.Item>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>{t('routes.targets')}</div>
          <p style={{ color: '#999', fontSize: 12, marginTop: 0 }}>{t('routes.targetHint')}</p>
          <Form.List name="targets">
            {(fields, { add, remove }) => (
              <>
                {fields.map(({ key, name: fidx, ...rest }) => (
                  <Space key={key} align="baseline" style={{ display: 'flex', marginBottom: 4 }} wrap>
                    <Form.Item {...rest} name={[fidx, 'deployment_id']} rules={[{ required: true, message: t('routes.targetReq') }]}
                      style={{ marginBottom: 0 }}>
                      <Select style={{ width: 260 }} showSearch optionFilterProp="label" placeholder={t('routes.targetPh')}
                        options={deploys.map((d: any) => ({ value: d.id, label: `${d.name} (${d.status})` }))} />
                    </Form.Item>
                    <Form.Item {...rest} name={[fidx, 'weight']} rules={[{ required: true }]} style={{ marginBottom: 0 }}
                      tooltip={t('routes.weightHint')}>
                      <InputNumber min={1} max={100} style={{ width: 110 }} />
                    </Form.Item>
                    <Form.Item {...rest} name={[fidx, 'is_fallback']} valuePropName="checked" style={{ marginBottom: 0 }}>
                      <Switch size="small" checkedChildren={t('routes.fallback')} />
                    </Form.Item>
                    {fields.length > 1 && (
                      <Button size="small" type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(fidx)} />
                    )}
                  </Space>
                ))}
                <Button type="dashed" block icon={<PlusOutlined />} onClick={() => add({ deployment_id: undefined, weight: 100, is_fallback: false })}>
                  {t('routes.addTarget')}
                </Button>
              </>
            )}
          </Form.List>
          {editId && (
            <p style={{ color: '#999', fontSize: 12 }}>
              {t('routes.nameLocked')}
            </p>
          )}
        </Form>
      </Modal>
    </div>
  )
}
