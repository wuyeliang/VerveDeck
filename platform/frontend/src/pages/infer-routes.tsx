import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Form, Input, InputNumber, Select, Drawer,
  message, Modal, Switch, Tooltip, Dropdown,
} from 'antd'
import {
  PlusOutlined, CopyOutlined, EditOutlined, DeleteOutlined,
  MinusCircleOutlined, ExclamationCircleOutlined,
  SearchOutlined, MoreOutlined, ReloadOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { StatusDot, SectionTitle } from './infer-shared'

/** 模型路由：服务名 → 多部署目标（权重 + 容灾），数据面走 Higress 网关 */
export default function InferRoutes() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [deploys, setDeploys] = useState<any[]>([])
  const [kw, setKw] = useState('')
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

  // 路由同步状态点：启用中=按同步态着色；停用=灰
  const syncState = (r: any) => {
    if (!r.enabled) return { text: t('common.disabled'), tone: 'default' as const }
    if (r.sync_status === 'error') return { text: t('routes.syncErr'), tone: 'error' as const }
    if (r.sync_status === 'pending') return { text: t('routes.pending'), tone: 'warning' as const }
    return { text: t('routes.synced'), tone: 'success' as const }
  }

  const onRowAction = (key: string, r: any) => {
    if (key === 'edit') { openEdit(r); return }
    if (key === 'delete') {
      Modal.confirm({
        title: t('routes.deleteConfirm'),
        okButtonProps: { danger: true },
        onOk: () => del(r.id),
      })
    }
  }

  const filtered = list.filter((r) => {
    const k = kw.trim().toLowerCase()
    return !k || (r.name || '').toLowerCase().includes(k) || (r.description || '').toLowerCase().includes(k)
  })

  return (
    <div>
      {/* 工具栏：左=搜索+刷新；右=新建（列表页无大标题） */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
        <Space wrap>
          <Input allowClear prefix={<SearchOutlined style={{ color: 'var(--ant-color-text-quaternary)' }} />}
            placeholder={t('routes.searchPh')} value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 260 }} />
          <Button type="text" icon={<ReloadOutlined />} onClick={load} />
        </Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t('routes.new')}</Button>
      </div>
      <Table rowKey="id" size="small" dataSource={filtered} pagination={{ pageSize: 12, showSizeChanger: false }}
        scroll={{ x: 1180 }}
        locale={{ emptyText: t('routes.empty') }}
        columns={[
          {
            title: t('common.name'), dataIndex: 'name', width: 220, fixed: 'left', render: (v: string, r: any) => (
              <div style={{ minWidth: 0 }}>
                <Space size={4}>
                  <span style={{ color: 'var(--ant-color-primary)', fontWeight: 600 }}>{v}</span>
                  <Button size="small" type="text" icon={<CopyOutlined />}
                    onClick={() => copy(`${location.origin}/v1/${r.name}`)} />
                </Space>
                <div style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)' }}>
                  <code style={{ fontSize: 11 }}>{r.endpoint}</code>
                </div>
              </div>
            ),
          },
          {
            title: t('routes.targets'), width: 340, render: (_: any, r: any) => (
              <Space size={[4, 4]} wrap>
                {(r.targets || []).length === 0 && <span style={{ color: 'var(--ant-color-text-quaternary)' }}>{t('routes.noTargets')}</span>}
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
            title: t('common.status'), width: 210, render: (_: any, r: any) => {
              const st = syncState(r)
              return (
                <Space size={6} wrap>
                  <Switch size="small" checked={r.enabled} onChange={(c) => setEnabled(r, c)}
                    checkedChildren={t('common.enabled')} unCheckedChildren={t('common.disabled')} />
                  <StatusDot status={st.text} tone={st.tone} tip={r.sync_message} />
                  {r.sync_message && (
                    <Tooltip title={r.sync_message}>
                      <ExclamationCircleOutlined style={{ color: r.sync_status === 'error' ? '#cf1322' : '#8c8c8c' }} />
                    </Tooltip>
                  )}
                </Space>
              )
            },
          },
          { title: t('common.desc'), dataIndex: 'description', ellipsis: true, render: (v: string) => v || <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span> },
          {
            title: t('common.actions'), key: 'ops', width: 52, fixed: 'right', align: 'center',
            render: (_: any, r: any) => (
              <Dropdown menu={{
                items: [
                  { key: 'edit', icon: <EditOutlined />, label: t('common.edit') },
                  { type: 'divider' as const },
                  { key: 'delete', icon: <DeleteOutlined />, label: t('common.delete'), danger: true },
                ],
                onClick: ({ key }: any) => onRowAction(key, r),
              }}>
                <Button size="small" type="text" icon={<MoreOutlined />} />
              </Dropdown>
            ),
          },
        ]} />

      {/* 新建 / 编辑路由（右侧 Drawer；名即 API 前缀，编辑时锁定） */}
      <Drawer title={editId ? t('routes.editTitle') : t('routes.new')} width={640} open={open} onClose={() => setOpen(false)}
        footer={<div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => setOpen(false)}>{t('common.cancel')}</Button>
            <Button type="primary" onClick={submit}>{t('common.confirm')}</Button>
          </Space>
        </div>}>
        <Form form={form} layout="vertical">
          <SectionTitle>{t('routes.name')}</SectionTitle>
          <Form.Item name="name"
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
          <SectionTitle>{t('routes.targets')}</SectionTitle>
          <p style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginTop: 0 }}>{t('routes.targetHint')}</p>
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
          <SectionTitle>{t('common.desc')}</SectionTitle>
          <Form.Item name="description">
            <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
          </Form.Item>
          {editId && (
            <p style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12 }}>
              {t('routes.nameLocked')}
            </p>
          )}
        </Form>
      </Drawer>
    </div>
  )
}
