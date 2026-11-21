import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Form, Input, InputNumber, Select, Drawer,
  message, Modal, Switch, Tooltip, Dropdown, Radio,
} from 'antd'
import {
  PlusOutlined, CopyOutlined, EditOutlined, DeleteOutlined,
  MinusCircleOutlined, ExclamationCircleOutlined,
  SearchOutlined, MoreOutlined, ReloadOutlined, ArrowUpOutlined, ArrowDownOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { StatusDot, SectionTitle } from './infer-shared'

/** 模型路由：服务名 → 多部署目标（负载均衡权重 / 优先级级联）+ 路由级缓存加速。
 *  部署创建时自动生成同名路由（auto），数据面走 Higress 网关（/v1/<名>/）。 */
export default function InferRoutes() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [deploys, setDeploys] = useState<any[]>([])
  const [caches, setCaches] = useState<any[]>([])
  const [kw, setKw] = useState('')
  const [open, setOpen] = useState(false)
  const [editId, setEditId] = useState<number | null>(null)
  const [form] = Form.useForm()

  const load = useCallback(() => {
    api.get('/api/inference/routes', { params: projectId ? { project_id: projectId } : {} })
      .then((r) => setList(r.data || [])).catch(() => {})
    api.get('/api/inference/deployments')
      .then((r) => setDeploys(r.data || [])).catch(() => {})
    api.get('/api/cache/services')
      .then((r) => setCaches(r.data || [])).catch(() => {})
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
    form.setFieldsValue({
      name: '', description: '', enabled: true, policy: 'lb',
      cache_service_id: undefined,
      targets: [{ deployment_id: undefined, weight: 100, is_fallback: false }],
    })
    setEditId(null)
    setOpen(true)
  }

  const openEdit = (r: any) => {
    form.resetFields()
    form.setFieldsValue({
      name: r.name, description: r.description || '',
      enabled: r.enabled, policy: r.policy || 'lb',
      cache_service_id: r.cache_service_id || undefined,
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
      // 路由名即对外 API 前缀（/v1/<name>/），编辑时锁定（换名请删除重建）
      await api.put(`/api/inference/routes/${editId}`, {
        description: v.description || '', enabled: v.enabled,
        policy: v.policy, cache_service_id: v.cache_service_id ?? null, targets,
      })
    } else {
      await api.post('/api/inference/routes', {
        project_id: projectId, name: v.name, description: v.description || '',
        enabled: v.enabled, policy: v.policy,
        cache_service_id: v.cache_service_id ?? null, targets,
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

  const policyTag = (r: any) => (r.policy === 'priority'
    ? <Tag color="purple">{t('routes.policyPriority')}</Tag>
    : <Tag color="blue">{t('routes.policyLb')}</Tag>)

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

  const policy = Form.useWatch('policy', form) || 'lb'

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
        scroll={{ x: 1320 }}
        locale={{ emptyText: t('routes.empty') }}
        columns={[
          {
            title: t('common.name'), dataIndex: 'name', width: 220, fixed: 'left', render: (v: string, r: any) => (
              <div style={{ minWidth: 0 }}>
                <Space size={4}>
                  <span style={{ color: 'var(--ant-color-primary)', fontWeight: 600 }}>{v}</span>
                  {r.auto && <Tag color="green">{t('routes.autoTag')}</Tag>}
                  <Button size="small" type="text" icon={<CopyOutlined />}
                    onClick={() => copy(`${location.origin}/v1/${r.name}`)} />
                </Space>
                <div style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)' }}>
                  <code style={{ fontSize: 11 }}>{r.endpoint}</code>
                </div>
              </div>
            ),
          },
          { title: t('routes.policy'), width: 120, render: (_: any, r: any) => policyTag(r) },
          {
            title: t('routes.models'), width: 330, render: (_: any, r: any) => (
              <Space size={[4, 4]} wrap>
                {(r.targets || []).length === 0 && <span style={{ color: 'var(--ant-color-text-quaternary)' }}>{t('routes.noTargets')}</span>}
                {(r.targets || []).map((x: any, i: number) => (
                  <Tag key={x.id} color={r.policy === 'priority' ? (i === 0 ? 'blue' : 'gold') : (x.is_fallback ? 'gold' : 'blue')}>
                    {r.policy === 'priority' ? `P${i + 1} ` : ''}{x.deployment_name}
                    {r.policy !== 'priority' && ` ${x.weight}`}
                    {r.policy !== 'priority' && x.is_fallback ? ` · ${t('routes.fallback')}` : ''}
                    {x.deployment_status !== 'running' && ` (${x.deployment_status})`}
                  </Tag>
                ))}
              </Space>
            ),
          },
          {
            title: t('routes.cacheSvc'), width: 140, render: (_: any, r: any) =>
              r.cache_service_id
                ? <Tag icon={<ThunderboltOutlined />} color="cyan">{r.cache_name || `#${r.cache_service_id}`}</Tag>
                : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>,
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
      <Drawer title={editId ? t('routes.editTitle') : t('routes.new')} width={680} open={open} onClose={() => setOpen(false)}
        footer={<div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => setOpen(false)}>{t('common.cancel')}</Button>
            <Button type="primary" onClick={submit}>{t('common.confirm')}</Button>
          </Space>
        </div>}>
        <Form form={form} layout="vertical">
          <SectionTitle>{t('routes.basic')}</SectionTitle>
          <div style={{ display: 'flex', gap: 12 }}>
            <Form.Item name="name" style={{ flex: 1, marginBottom: 12 }}
              tooltip={editId ? t('routes.nameLocked') : undefined}
              rules={[{ required: true, pattern: /^[a-z0-9][a-z0-9-]*$/, message: t('routes.nameRule') }, { max: 128 }]}>
              <Input placeholder={t('routes.namePh')} showCount maxLength={128}
                disabled={!!editId} />
            </Form.Item>
            <Form.Item name="enabled" label={t('common.status')} valuePropName="checked" style={{ width: 160, marginBottom: 12 }}>
              <Switch checkedChildren={t('common.enabled')} unCheckedChildren={t('common.disabled')} />
            </Form.Item>
          </div>
          <Form.Item name="description" label={t('common.desc')}>
            <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
          </Form.Item>

          <SectionTitle>{t('routes.models')}</SectionTitle>
          <Form.Item name="policy" tooltip={t('routes.policyTip')}>
            <Radio.Group optionType="button" buttonStyle="solid">
              <Radio.Button value="lb">{t('routes.policyLb')}</Radio.Button>
              <Radio.Button value="priority">{t('routes.policyPriority')}</Radio.Button>
            </Radio.Group>
          </Form.Item>
          <p style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginTop: 0, marginBottom: 8 }}>
            {policy === 'priority' ? t('routes.priorityHint') : t('routes.lbHint')}
          </p>
          <Form.List name="targets">
            {(fields, { add, remove, move }) => (
              <>
                {fields.map(({ key, name: fidx, ...rest }, i) => (
                  <Space key={key} align="baseline" style={{ display: 'flex', marginBottom: 4 }} wrap>
                    {policy === 'priority' && (
                      <Tag color={i === 0 ? 'blue' : 'gold'} style={{ margin: 0, minWidth: 30, textAlign: 'center' }}>P{i + 1}</Tag>
                    )}
                    <Form.Item {...rest} name={[fidx, 'deployment_id']} rules={[{ required: true, message: t('routes.targetReq') }]}
                      style={{ marginBottom: 0 }}>
                      <Select style={{ width: 240 }} showSearch optionFilterProp="label" placeholder={t('routes.targetPh')}
                        options={deploys.map((d: any) => ({ value: d.id, label: `${d.name} (${d.status})` }))} />
                    </Form.Item>
                    {policy === 'lb' && (
                      <>
                        <Form.Item {...rest} name={[fidx, 'weight']} rules={[{ required: true }]} style={{ marginBottom: 0 }}
                          tooltip={t('routes.weightHint')}>
                          <InputNumber min={1} max={100} style={{ width: 100 }} />
                        </Form.Item>
                        <Form.Item {...rest} name={[fidx, 'is_fallback']} valuePropName="checked" style={{ marginBottom: 0 }}>
                          <Switch size="small" checkedChildren={t('routes.fallback')} />
                        </Form.Item>
                      </>
                    )}
                    {policy === 'priority' && (
                      <Space size={0}>
                        <Button size="small" type="text" icon={<ArrowUpOutlined />} disabled={i === 0}
                          onClick={() => move(fidx, fidx - 1)} />
                        <Button size="small" type="text" icon={<ArrowDownOutlined />} disabled={i === fields.length - 1}
                          onClick={() => move(fidx, fidx + 1)} />
                      </Space>
                    )}
                    {fields.length > 1 && (
                      <Button size="small" type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(fidx)} />
                    )}
                  </Space>
                ))}
                <Button type="dashed" block icon={<PlusOutlined />}
                  onClick={() => add({ deployment_id: undefined, weight: 100, is_fallback: false })}>
                  {t('routes.addTarget')}
                </Button>
              </>
            )}
          </Form.List>

          <SectionTitle>{t('routes.cacheSvc')}</SectionTitle>
          <Form.Item name="cache_service_id" tooltip={t('routes.cacheTip')}>
            <Select allowClear showSearch optionFilterProp="label" placeholder={t('infer.cacheSvcNone')}
              options={caches.map((c: any) => ({ value: c.id, label: `${c.name} (${c.capacity_gb ? `${c.capacity_gb} GB ` : ''}${c.cache_type === 'ssd' ? t('cache.typeSsd') : t('cache.typeMemory')})` }))} />
          </Form.Item>
        </Form>
      </Drawer>
    </div>
  )
}
