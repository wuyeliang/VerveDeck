import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select,
  message, Popconfirm, Card, Tooltip, Divider,
} from 'antd'
import {
  PlusOutlined, PlayCircleOutlined, PoweroffOutlined, EditOutlined,
  DeleteOutlined, ExclamationCircleOutlined, DatabaseOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const ST: Record<string, string> = { stopped: 'default', creating: 'processing', running: 'success', failed: 'error' }

/** 缓存加速：① 缓存服务实例（托管容器生命周期）② 部署级扩展 KV（CPU 卸载 / HiCache） */
export default function InferCache() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [deploys, setDeploys] = useState<any[]>([])
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

  // ---------- 缓存服务 创建 / 编辑 ----------
  const submitNew = async () => {
    const v = await form.validateFields()
    const env = parseJsonField(v.env, 'env')
    if (env === null) return
    await api.post('/api/cache/services', {
      name: v.name, description: v.description || '', image: v.image,
      command: v.command || '', env, port: v.port ?? 0,
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
      description: v.description || '', image: v.image,
      command: v.command || '', env, port: v.port ?? 0,
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

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('cache.title')}</h3>
        <Button type="primary" icon={<PlusOutlined />}
          onClick={() => { form.resetFields(); form.setFieldsValue({ port: 0 }); setOpenNew(true) }}>
          {t('cache.new')}
        </Button>
      </Space>
      <p style={{ color: '#888', fontSize: 12, marginTop: 0 }}>{t('cache.hint')}</p>

      {/* ① 缓存服务实例 */}
      <Card size="small" title={<span><DatabaseOutlined /> {t('cache.svcTitle')}</span>} style={{ marginBottom: 16 }}>
        <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 10 }} scroll={{ x: 1080 }}
          locale={{ emptyText: t('cache.empty') }}
          columns={[
            { title: t('common.name'), dataIndex: 'name', width: 170, ellipsis: true, render: (v: string) => <span style={{ fontWeight: 600 }}>{v}</span> },
            { title: t('backends.image'), dataIndex: 'image', width: 250, ellipsis: true, render: (v: string) => <code style={{ fontSize: 11 }}>{v}</code> },
            {
              title: t('cache.command'), width: 180, ellipsis: true, render: (_: any, r: any) => r.command
                ? <Tooltip title={r.command}><code style={{ fontSize: 11 }}>{r.command}</code></Tooltip>
                : <Tooltip title={t('cache.cmdEmpty')}>{t('cache.imageEntry')}</Tooltip>,
            },
            { title: t('cache.port'), dataIndex: 'port', width: 70, render: (v: number) => v || <span style={{ color: '#bbb' }}>-</span> },
            {
              title: t('common.status'), dataIndex: 'status', width: 150, render: (v: string, r: any) => (
                <Space size={4}>
                  <Tag color={ST[v] || 'default'}>{v}</Tag>
                  {r.state_message && <Tooltip title={r.state_message}>
                    <ExclamationCircleOutlined style={{ color: v === 'failed' ? '#cf1322' : '#fa8c16' }} />
                  </Tooltip>}
                </Space>
              ),
            },
            { title: t('cache.endpoint'), width: 160, ellipsis: true, render: (_: any, r: any) =>
              r.url ? <code style={{ fontSize: 11 }}>{r.url}</code> : <span style={{ color: '#bbb' }}>-</span> },
            {
              title: t('cache.usedBy'), width: 170, render: (_: any, r: any) => (
                <Space size={[4, 4]} wrap>
                  {(r.used_by || []).length === 0 && <span style={{ color: '#bbb' }}>-</span>}
                  {(r.used_by || []).map((n: string) => <Tag key={n}>{n}</Tag>)}
                </Space>
              ),
            },
            {
              title: t('common.actions'), width: 190, render: (_: any, r: any) => (
                <Space size={4}>
                  {r.status !== 'running' && r.status !== 'creating' && (
                    <Button size="small" icon={<PlayCircleOutlined />} onClick={() => start(r)}>{t('cache.start')}</Button>
                  )}
                  {r.status === 'running' && (
                    <Popconfirm title={t('cache.stopConfirm')} okButtonProps={{ danger: true }} onConfirm={() => stop(r)}>
                      <Button size="small" danger icon={<PoweroffOutlined />}>{t('cache.stop')}</Button>
                    </Popconfirm>
                  )}
                  <Button size="small" icon={<EditOutlined />} onClick={() => {
                    editForm.resetFields()
                    editForm.setFieldsValue({
                      description: r.description || '', image: r.image,
                      command: r.command || '', env: jsonOf(r.env), port: r.port ?? 0,
                    })
                    setOpenEdit(r)
                  }}>{t('common.edit')}</Button>
                  <Popconfirm title={t('cache.delConfirm')} okButtonProps={{ danger: true }} onConfirm={() => del(r)}>
                    <Button size="small" danger icon={<DeleteOutlined />}>{t('common.delete')}</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]} />
      </Card>

      {/* ② 部署级扩展 KV */}
      <Card size="small" title={<span><DatabaseOutlined /> {t('cache.deployKvTitle')}</span>}>
        <p style={{ color: '#888', fontSize: 12, marginTop: 0, marginBottom: 8 }}>{t('cache.deployKvHint')}</p>
        <Table rowKey="id" size="small" dataSource={deploys} pagination={{ pageSize: 10 }} scroll={{ x: 900 }}
          columns={[
            { title: t('common.name'), dataIndex: 'name', width: 180, ellipsis: true, render: (v: string) => <span style={{ fontWeight: 600 }}>{v}</span> },
            { title: t('infer.colEngine'), dataIndex: 'engine', width: 90, render: (v: string) => <Tag color={v === 'vllm' ? 'blue' : 'cyan'}>{v}</Tag> },
            { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={ST[v] || 'default'}>{v}</Tag> },
            { title: t('cache.extKv'), width: 170, render: (_: any, r: any) => kvTag(r) || <span style={{ color: '#bbb' }}>{t('cache.none')}</span> },
            {
              title: t('cache.cacheSvc'), width: 170, render: (_: any, r: any) => {
                const c = list.find((x: any) => x.id === r.cache_service_id)
                return c
                  ? <Space size={4}><Tag color="cyan">{c.name}</Tag>{c.status !== 'running' && <Tag color="orange">{c.status}</Tag>}</Space>
                  : <span style={{ color: '#bbb' }}>-</span>
              },
            },
            {
              title: t('common.actions'), width: 100, render: (_: any, r: any) => (
                <Button size="small" icon={<EditOutlined />} onClick={() => openKvEdit(r)}>{t('cache.editKv')}</Button>
              ),
            },
          ]} />
      </Card>

      {/* 新建缓存服务 */}
      <Modal title={t('cache.new')} open={openNew} onCancel={() => setOpenNew(false)}
        onOk={submitNew} okText={t('common.confirm')} width={640}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('common.name')} rules={[{ required: true }]} style={{ maxWidth: 360 }}>
            <Input maxLength={128} showCount />
          </Form.Item>
          <Form.Item name="description" label={t('common.desc')}>
            <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
          </Form.Item>
          <Form.Item name="image" label={t('backends.image')} rules={[{ required: true }]}>
            <Input placeholder="registry.example.com/kv-cache:v1" />
          </Form.Item>
          <Form.Item name="command" label={t('cache.command')} tooltip={t('cache.cmdHint')}>
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} style={{ fontFamily: 'monospace', fontSize: 12 }}
              placeholder="python3 -m kvserve --port 8081" />
          </Form.Item>
          <Space wrap align="start">
            <Form.Item name="env" label={t('backends.env')} tooltip={t('backends.jsonInvalidHint')}>
              <Input.TextArea placeholder='{"KEY": "value"}' autoSize={{ minRows: 1, maxRows: 3 }} style={{ fontFamily: 'monospace', fontSize: 12, width: 320 }} />
            </Form.Item>
            <Form.Item name="port" label={t('cache.port')} tooltip={t('cache.portHint')}>
              <InputNumber min={0} max={65535} style={{ width: 140 }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      {/* 编辑缓存服务（运行中修改=自动重启） */}
      <Modal title={openEdit ? `${t('common.edit')} · ${openEdit.name}` : t('common.edit')}
        open={!!openEdit} onCancel={() => setOpenEdit(null)} onOk={submitEdit} okText={t('common.confirm')} width={640}>
        <Form form={editForm} layout="vertical">
          <Form.Item name="description" label={t('common.desc')}>
            <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
          </Form.Item>
          <Form.Item name="image" label={t('backends.image')} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="command" label={t('cache.command')} tooltip={t('cache.cmdHint')}>
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 3 }} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
          <Space wrap align="start">
            <Form.Item name="env" label={t('backends.env')} tooltip={t('backends.jsonInvalidHint')}>
              <Input.TextArea placeholder='{"KEY": "value"}' autoSize={{ minRows: 1, maxRows: 3 }} style={{ fontFamily: 'monospace', fontSize: 12, width: 320 }} />
            </Form.Item>
            <Form.Item name="port" label={t('cache.port')} tooltip={t('cache.portHint')}>
              <InputNumber min={0} max={65535} style={{ width: 140 }} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      {/* 部署级扩展 KV 编辑（触发实例重建） */}
      <Modal title={openKv ? `${t('cache.editKvFor', { name: openKv.name })}` : t('cache.editKv')}
        open={!!openKv} onCancel={() => setOpenKv(null)} onOk={submitKv} okText={t('common.confirm')} width={520}>
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
          <p style={{ color: '#999', fontSize: 12 }}>{t('cache.kvRebuildHint')}</p>
        </Form>
      </Modal>
    </div>
  )
}
