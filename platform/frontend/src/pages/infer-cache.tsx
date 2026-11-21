import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Form, Input, InputNumber, Select, Drawer,
  message, Modal, Card, Tooltip, Divider, Dropdown, Collapse,
} from 'antd'
import {
  PlusOutlined, PlayCircleOutlined, PoweroffOutlined, EditOutlined, DeleteOutlined,
  DatabaseOutlined, ThunderboltOutlined,
  SearchOutlined, MoreOutlined, ReloadOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { StatusDot, STATUS_TONE, SectionTitle } from './infer-shared'

/** 缓存加速：① 缓存服务（类型 内存|SSD + 容量 + 可选托管容器端点）
 *  ② 部署级扩展 KV（CPU 卸载 / HiCache，随实例重建生效） */
export default function InferCache() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [deploys, setDeploys] = useState<any[]>([])
  const [kw, setKw] = useState('')
  const [openNew, setOpenNew] = useState(false)
  const [openEdit, setOpenEdit] = useState<any>(null)
  const [openKv, setOpenKv] = useState<any>(null)  // 部署级扩展 KV 编辑
  const [form] = Form.useForm()
  const [editForm] = Form.useForm()
  const [kvForm] = Form.useForm()

  const load = useCallback(() => {
    api.get('/api/cache/services').then((r) => setList(r.data || [])).catch(() => {})
    api.get('/api/inference/deployments').then((r) => setDeploys(r.data || [])).catch(() => {})
  }, [])
  useEffect(() => { load(); const x = setInterval(load, 5000); return () => clearInterval(x) }, [load])

  const parseJsonField = (raw: string | undefined, label: string): Record<string, any> | null => {
    if (!raw || !raw.trim()) return {}
    try {
      const o = JSON.parse(raw)
      if (o && typeof o === 'object' && !Array.isArray(o)) return o
      message.error(`${label}: ${t('backends.jsonInvalid')}`)
      return null
    } catch {
      message.error(`${label}: ${t('backends.jsonInvalid')}`)
      return null
    }
  }
  const jsonOf = (o: any) => (o && Object.keys(o).length ? JSON.stringify(o, null, 2) : '')

  // 逻辑型缓存（未配置镜像）展示态
  const effStatus = (c: any) => c.image ? c.status : 'available'

  // ---------- 缓存服务 创建 / 编辑 ----------
  const submitNew = async () => {
    const v = await form.validateFields()
    const env = parseJsonField(v.env, 'env')
    if (env === null) return
    await api.post('/api/cache/services', {
      name: v.name, description: v.description || '',
      cache_type: v.cache_type || 'memory', capacity_gb: v.capacity_gb ?? 0,
      image: v.image || '', command: v.command || '', env, port: v.port ?? 0,
    })
    message.success(t('cache.created'))
    setOpenNew(false)
    load()
  }

  const submitEdit = async () => {
    const c = openEdit
    const v = await editForm.validateFields()
    const env = parseJsonField(v.env, 'env')
    if (env === null) return
    await api.put(`/api/cache/services/${c.id}`, {
      description: v.description || '',
      cache_type: v.cache_type || 'memory', capacity_gb: v.capacity_gb ?? 0,
      image: v.image || '', command: v.command || '', env, port: v.port ?? 0,
    })
    message.success(c.status === 'running' || c.status === 'creating'
      ? t('cache.restarting') : t('cache.saved'))
    setOpenEdit(null)
    load()
  }

  const start = (c: any) => {
    api.post(`/api/cache/services/${c.id}/start`).then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }
  const stop = (c: any) => {
    api.post(`/api/cache/services/${c.id}/stop`).then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }
  const del = (c: any) => {
    api.delete(`/api/cache/services/${c.id}`).then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  const openEditSvc = (r: any) => {
    editForm.resetFields()
    editForm.setFieldsValue({
      description: r.description || '',
      cache_type: r.cache_type || 'memory', capacity_gb: r.capacity_gb ?? 0,
      image: r.image || '', command: r.command || '', env: jsonOf(r.env), port: r.port ?? 0,
    })
    setOpenEdit(r)
  }

  // 行操作（⋯ 菜单）：启动 / 停止 / 编辑 / 删除（逻辑型无启停）
  const onRowAction = (key: string, r: any) => {
    if (key === 'start') { start(r); return }
    if (key === 'stop') {
      Modal.confirm({ title: t('cache.stopConfirm'), okButtonProps: { danger: true }, onOk: () => stop(r) })
      return
    }
    if (key === 'edit') { openEditSvc(r); return }
    if (key === 'delete') {
      Modal.confirm({ title: t('cache.delConfirm'), okButtonProps: { danger: true }, onOk: () => del(r) })
    }
  }

  // ---------- 部署级扩展 KV 快速编辑 ----------
  const openKvEdit = (d: any) => {
    kvForm.resetFields()
    kvForm.setFieldsValue({
      engine: d.engine,
      cpu_offload_gb: d.kv_cache_ext?.cpu_offload_gb ?? undefined,
      hicache_size: d.kv_cache_ext?.hicache_size ?? undefined,
      cache_service_id: d.cache_service_id || undefined,
    })
    setOpenKv(d)
  }
  const submitKv = async () => {
    const d = openKv
    const v = await kvForm.validateFields()
    const kv: Record<string, number> = {}
    if (v.engine === 'sglang') {
      if (v.hicache_size != null && v.hicache_size !== '') kv.hicache_size = v.hicache_size
    } else if (v.cpu_offload_gb != null && v.cpu_offload_gb !== '') {
      kv.cpu_offload_gb = v.cpu_offload_gb
    }
    await api.put(`/api/inference/deployments/${d.id}`, {
      kv_cache_ext: kv, cache_service_id: v.cache_service_id ?? null,
    })
    message.success(t('cache.kvSaved'))
    setOpenKv(null)
    load()
  }

  const kvTag = (d: any) => {
    const kv = d.kv_cache_ext || {}
    if (d.engine === 'sglang') {
      return kv.hicache_size ? <Tag color="cyan">{t('cache.hicache')}: {kv.hicache_size} GB</Tag> : null
    }
    return kv.cpu_offload_gb ? <Tag color="cyan">{t('cache.cpuOffload')}: {kv.cpu_offload_gb} GB</Tag> : null
  }

  const typeTag = (v: string) => v === 'ssd'
    ? <Tag color="cyan">{t('cache.typeSsd')}</Tag>
    : <Tag color="blue">{t('cache.typeMemory')}</Tag>

  const filtered = list.filter((r) => {
    const k = kw.trim().toLowerCase()
    return !k || (r.name || '').toLowerCase().includes(k) || (r.image || '').toLowerCase().includes(k)
  })

  return (
    <div>
      {/* 工具栏：左=搜索+刷新；右=新建（列表页无大标题） */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
        <Space wrap>
          <Input allowClear prefix={<SearchOutlined style={{ color: 'var(--ant-color-text-quaternary)' }} />}
            placeholder={t('cache.searchPh')} value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 260 }} />
          <Button type="text" icon={<ReloadOutlined />} onClick={load} />
        </Space>
        <Button type="primary" icon={<PlusOutlined />}
          onClick={() => { form.resetFields(); form.setFieldsValue({ cache_type: 'memory', capacity_gb: 0, port: 0 }); setOpenNew(true) }}>
          {t('cache.new')}
        </Button>
      </div>

      {/* ① 缓存服务 */}
      <Card size="small" title={<span><DatabaseOutlined /> {t('cache.svcTitle')}</span>} style={{ marginBottom: 16 }}>
        <Table rowKey="id" size="small" dataSource={filtered} pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 1080 }}
          locale={{ emptyText: t('cache.empty') }}
          columns={[
            {
              title: t('common.status'), dataIndex: 'status', width: 130,
              render: (v: string, r: any) => {
                const s = effStatus(r)
                const tone = s === 'available' ? 'success' : (STATUS_TONE[s] || 'default')
                const tip = r.image ? r.state_message : t('cache.logicalTip')
                return <StatusDot status={s === 'available' ? t('cache.available') : s} tone={tone} tip={tip} />
              },
            },
            { title: t('common.name'), dataIndex: 'name', width: 190, ellipsis: true, render: (v: string, r: any) => (
              <Space size={4}>
                <span style={{ color: 'var(--ant-color-primary)', fontWeight: 600 }}>{v}</span>
                {r.image && <Tag style={{ margin: 0 }}>{t('cache.containerTag')}</Tag>}
              </Space>
            ) },
            { title: t('cache.type'), dataIndex: 'cache_type', width: 100, render: (v: string) => typeTag(v) },
            {
              title: t('cache.capacity'), width: 100,
              render: (_: any, r: any) => r.capacity_gb
                ? `${r.capacity_gb} GB`
                : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>,
            },
            { title: t('common.desc'), dataIndex: 'description', ellipsis: true, render: (v: string) => v || <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span> },
            {
              title: t('cache.usedBy'), width: 170, render: (_: any, r: any) => (
                <Space size={[4, 4]} wrap>
                  {(r.used_by || []).length === 0 && <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>}
                  {(r.used_by || []).map((n: string) => <Tag key={n}>{n}</Tag>)}
                </Space>
              ),
            },
            {
              title: t('cache.endpoint'), width: 150, ellipsis: true, render: (_: any, r: any) =>
                r.url ? <code style={{ fontSize: 11 }}>{r.url}</code>
                  : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>,
            },
            {
              title: t('common.actions'), key: 'ops', width: 52, fixed: 'right', align: 'center',
              render: (_: any, r: any) => (
                <Dropdown menu={{
                  items: [
                    ...((r.image && r.status !== 'running' && r.status !== 'creating')
                      ? [{ key: 'start', icon: <PlayCircleOutlined />, label: t('cache.start') }]
                      : []),
                    ...((r.image && r.status === 'running')
                      ? [{ key: 'stop', icon: <PoweroffOutlined />, label: t('cache.stop'), danger: true }]
                      : []),
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
      </Card>

      {/* ② 部署级扩展 KV */}
      <Card size="small" title={<span><ThunderboltOutlined /> {t('cache.deployKvTitle')}</span>}>
        <p style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12, marginTop: 0, marginBottom: 8 }}>{t('cache.deployKvHint')}</p>
        <Table rowKey="id" size="small" dataSource={deploys} pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 900 }}
          columns={[
            { title: t('common.name'), dataIndex: 'name', width: 180, ellipsis: true, render: (v: string) => (
              <span style={{ color: 'var(--ant-color-primary)', fontWeight: 600 }}>{v}</span>
            ) },
            { title: t('infer.colEngine'), dataIndex: 'engine', width: 90, render: (v: string) => <Tag color={v === 'vllm' ? 'blue' : 'cyan'}>{v}</Tag> },
            {
              title: t('common.status'), dataIndex: 'status', width: 110, render: (v: string, r: any) => (
                <StatusDot status={v} tone={STATUS_TONE[v] || 'default'} tip={r.state_message} />
              ),
            },
            { title: t('cache.extKv'), width: 170, render: (_: any, r: any) => kvTag(r) || <span style={{ color: 'var(--ant-color-text-quaternary)' }}>{t('cache.none')}</span> },
            {
              title: t('cache.cacheSvc'), width: 170, render: (_: any, r: any) => {
                const c = list.find((x: any) => x.id === r.cache_service_id)
                return c
                  ? <Space size={4}><Tag color="cyan">{c.name}</Tag>{c.status !== 'running' && <Tag color="orange">{c.status}</Tag>}</Space>
                  : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>
              },
            },
            {
              title: t('common.actions'), key: 'ops', width: 100, align: 'center',
              render: (_: any, r: any) => (
                <Button size="small" icon={<EditOutlined />} onClick={() => openKvEdit(r)}>
                  {t('cache.editKv')}
                </Button>
              ),
            },
          ]} />
      </Card>

      {/* 新建 / 编辑缓存服务：名称/类型/容量/说明 + 高级（镜像容器） */}
      {([
        {
          key: 'new',
          open: openNew,
          onClose: () => setOpenNew(false),
          f: form,
          title: t('cache.new'),
          submit: submitNew,
          withName: true,
        },
        {
          key: 'edit',
          open: !!openEdit,
          onClose: () => setOpenEdit(null),
          f: editForm,
          title: openEdit ? `${t('common.edit')} · ${openEdit.name}` : t('common.edit'),
          submit: submitEdit,
          withName: false,
        },
      ]).map(({ key, open, onClose, f, title, submit, withName }: any) => (
        <Drawer key={key} title={title} width={560} open={open} onClose={onClose}
          footer={<div style={{ textAlign: 'right' }}>
            <Space>
              <Button onClick={onClose}>{t('common.cancel')}</Button>
              <Button type="primary" onClick={submit}>{t('common.confirm')}</Button>
            </Space>
          </div>}>
          <Form form={f} layout="vertical">
            {withName && (
              <Form.Item name="name" label={t('common.name')} rules={[{ required: true }]}>
                <Input maxLength={128} showCount />
              </Form.Item>
            )}
            <Form.Item name="cache_type" label={t('cache.type')} rules={[{ required: true }]}>
              <Select options={[
                { value: 'memory', label: t('cache.typeMemory') },
                { value: 'ssd', label: t('cache.typeSsd') },
              ]} />
            </Form.Item>
            <Form.Item name="capacity_gb" label={t('cache.capacity')} rules={[{ required: true }]}>
              <InputNumber min={0} max={65536} addonAfter="GB" style={{ width: 220 }} />
            </Form.Item>
            <Form.Item name="description" label={t('common.desc')}>
              <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
            </Form.Item>
            <Collapse ghost items={[{
              key: 'adv',
              label: <span style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)' }}>{t('cache.advTitle')}</span>,
              children: (
                <>
                  <p style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginTop: 0 }}>{t('cache.advHint')}</p>
                  <Form.Item name="image" label={t('backends.image')} tooltip={t('cache.imageHint')}>
                    <Input placeholder="registry.example.com/kv-cache:v1" />
                  </Form.Item>
                  <Form.Item name="command" label={t('cache.command')} tooltip={t('cache.cmdHint')}>
                    <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} style={{ fontFamily: 'monospace', fontSize: 12 }}
                      placeholder="python3 -m kvserve --port 8081" />
                  </Form.Item>
                  <Space wrap align="start">
                    <Form.Item name="env" label={t('backends.env')} tooltip={t('backends.jsonInvalidHint')}>
                      <Input.TextArea placeholder='{"KEY": "value"}' autoSize={{ minRows: 1, maxRows: 3 }} style={{ fontFamily: 'monospace', fontSize: 12, width: 300 }} />
                    </Form.Item>
                    <Form.Item name="port" label={t('cache.port')} tooltip={t('cache.portHint')}>
                      <InputNumber min={0} max={65535} style={{ width: 140 }} />
                    </Form.Item>
                  </Space>
                </>
              ),
            }]} />
          </Form>
        </Drawer>
      ))}

      {/* 部署级扩展 KV 编辑（触发实例重建） */}
      <Drawer title={openKv ? `${t('cache.editKvFor', { name: openKv.name })}` : t('cache.editKv')}
        width={520} open={!!openKv} onClose={() => setOpenKv(null)}
        footer={<div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => setOpenKv(null)}>{t('common.cancel')}</Button>
            <Button type="primary" onClick={submitKv}>{t('common.confirm')}</Button>
          </Space>
        </div>}>
        <Form form={kvForm} layout="vertical">
          <Form.Item name="engine" noStyle>
            <Input type="hidden" />
          </Form.Item>
          {openKv?.engine === 'sglang' ? (
            <Form.Item name="hicache_size" label={t('infer.hicacheSize')} tooltip={t('infer.hicacheSizeHint')}>
              <InputNumber min={1} max={512} placeholder={t('cache.none')} addonAfter="GB" style={{ width: 220 }} />
            </Form.Item>
          ) : (
            <Form.Item name="cpu_offload_gb" label={t('infer.cpuOffloadGb')} tooltip={t('infer.cpuOffloadGbHint')}>
              <InputNumber min={1} max={256} placeholder={t('cache.none')} addonAfter="GB" style={{ width: 220 }} />
            </Form.Item>
          )}
          <Divider style={{ margin: '12px 0' }} />
          <Form.Item name="cache_service_id" label={t('cache.cacheSvc')} tooltip={t('cache.svcHint')}>
            <Select allowClear showSearch optionFilterProp="label" placeholder={t('infer.cacheSvcNone')}
              options={list.map((c: any) => ({ value: c.id, label: `${c.name} (${c.status})` }))} />
          </Form.Item>
          <p style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12 }}>{t('cache.kvRebuildHint')}</p>
        </Form>
      </Drawer>
    </div>
  )
}
