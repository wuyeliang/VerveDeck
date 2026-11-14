import React, { useEffect, useState } from 'react'
import { Row, Col, Card, Tag, Table, Button, Space, Progress, Popconfirm, Tooltip,
  Modal, Form, Input, Select, message } from 'antd'
import { DeleteOutlined, HomeOutlined, ReloadOutlined, ArrowLeftOutlined, PlusOutlined,
  ApiOutlined, HddOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

function fmtBytes(n: number): string {
  if (n === undefined || n === null) return '-'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  n = Number(n) || 0
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`
}

const TYPE_COLOR: Record<string, string> = { object: 'blue', local: 'green', nfs: 'purple', block: 'orange' }

/** 存储管理（一级菜单 /storage）：多类型存储后端 + 主对象存储状态 + 对象浏览 */
export default function StoragePage() {
  const { t } = useTranslation()
  const [ov, setOv] = useState<any>(null)
  const [backends, setBackends] = useState<any[]>([])
  const [prefix, setPrefix] = useState('')
  const [backendId, setBackendId] = useState(0)  // 0 = 主对象存储（rustfs）
  const [items, setItems] = useState<any[]>([])
  const [addOpen, setAddOpen] = useState(false)
  const [form] = Form.useForm()
  const [bType, setBType] = useState('object')

  const load = () => {
    api.get('/api/storage').then((r) => setOv(r.data)).catch(() => {})
    api.get('/api/storage/backends').then((r) => setBackends(r.data || [])).catch(() => setBackends([]))
    api.get('/api/storage/objects', { params: { prefix, backend: backendId } })
      .then((r) => setItems(r.data.items || [])).catch(() => setItems([]))
  }
  // prefix/后端变化重新拉取；其余 10s 轮询（状态本身即连通性检查）
  useEffect(() => { load(); const x = setInterval(load, 10000); return () => clearInterval(x) }, [prefix, backendId])

  const enabled = !!ov?.enabled
  const ok = ov?.ok !== false
  const diskPct = ov?.disk_total ? Math.round(((ov.disk_used || 0) / ov.disk_total) * 100) : 0
  const parts = prefix ? prefix.split('/').filter(Boolean) : []
  const goUp = () => {
    if (parts.length <= 1) setPrefix('')
    else setPrefix(parts.slice(0, -1).join('/') + '/')
  }
  const objectBackends = backends.filter((b) => b.type === 'object')

  const delObjects = async (p: string) => {
    try {
      const r = await api.delete('/api/storage/objects', { params: { prefix: p, backend: backendId } })
      message.success(`${t('storage.delOk')}: ${r.data.deleted}`)
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('storage.delFail'))
    }
  }
  const addBackend = async () => {
    try {
      const v = await form.validateFields()
      const cfg: any = {}
      if (v.type === 'object') {
        Object.assign(cfg, { endpoint: v.endpoint, access_key: v.access_key || '', secret_key: v.secret_key || '', bucket: v.bucket })
      } else {
        Object.assign(cfg, { path: v.path })
        if (v.type === 'nfs') cfg.server = v.server || ''
        if (v.type === 'block') cfg.device = v.device || ''
      }
      const r = await api.post('/api/storage/backends', { name: v.name, type: v.type, config: cfg })
      message.success(r.data.status === 'ok' ? t('storage.addOk') : `${t('storage.addOk')}: ${r.data.detail}`)
      setAddOpen(false); form.resetFields(); load()
    } catch (e: any) {
      if (e?.errorFields) return
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    }
  }
  const testBackend = async (id: number) => {
    try {
      const r = await api.post(`/api/storage/backends/${id}/test`)
      message.success(`${r.data.name}: ${r.data.detail}`)
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    }
  }
  const delBackend = async (id: number) => {
    try {
      await api.delete(`/api/storage/backends/${id}`)
      if (backendId === id) setBackendId(0)
      message.success(t('common.deleted'))
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    }
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('nav.storage')}</h3>
      <Row gutter={16}>
        <Col span={24}>
          <Card size="small" title={t('storage.backends')}
            extra={<Button type="primary" size="small" icon={<PlusOutlined />}
              onClick={() => { form.resetFields(); setBType('object'); setAddOpen(true) }}>{t('storage.addBackend')}</Button>}>
            <Table size="small" rowKey="id" dataSource={backends} pagination={false}
              locale={{ emptyText: t('storage.backendEmpty') }}
              columns={[
                { title: t('common.name'), dataIndex: 'name', width: 140 },
                {
                  title: t('storage.colType'), dataIndex: 'type', width: 100,
                  render: (v: string) => <Tag color={TYPE_COLOR[v] || 'default'}>{t(`storage.type${v[0].toUpperCase()}${v.slice(1)}`, { defaultValue: v })}</Tag>,
                },
                {
                  title: t('storage.colConfig'), dataIndex: 'config', ellipsis: true,
                  render: (c: any) => <code style={{ fontSize: 11 }}>
                    {c?.endpoint ? `${c.endpoint}/${c.bucket}` : c?.path || ''}
                    {c?.server ? ` (${c.server})` : ''}{c?.device ? ` (${c.device})` : ''}
                  </code>,
                },
                {
                  title: t('common.status'), dataIndex: 'status', width: 90,
                  render: (v: string, r: any) => (
                    <Tooltip title={r.detail}><Tag color={v === 'ok' ? 'success' : v === 'error' ? 'error' : 'default'}>{t(`storage.st${v[0].toUpperCase()}${v.slice(1)}`, { defaultValue: v })}</Tag></Tooltip>
                  ),
                },
                {
                  title: t('storage.colCapacity'), width: 180,
                  render: (_: any, r: any) => r.size_total
                    ? <div>
                        <Progress percent={Math.round((r.size_used / r.size_total) * 100)} size="small"
                          format={() => `${fmtBytes(r.size_used)} / ${fmtBytes(r.size_total)}`} />
                      </div>
                    : (r.type === 'object' ? <span style={{ fontSize: 12, color: '#888' }}>{r.detail || '-'}</span> : '-'),
                },
                {
                  title: t('common.actions'), width: 130,
                  render: (_: any, r: any) => (
                    <Space size={4}>
                      <Button size="small" icon={<ApiOutlined />} onClick={() => testBackend(r.id)}>{t('storage.test')}</Button>
                      <Popconfirm title={t('storage.delBackendConfirm')} onConfirm={() => delBackend(r.id)}>
                        <Button size="small" danger icon={<DeleteOutlined />} />
                      </Popconfirm>
                    </Space>
                  ),
                },
              ]} />
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={10}>
          <Card size="small" title={t('storage.statusTitle')}
            extra={<Button size="small" icon={<ReloadOutlined />} onClick={load}>{t('storage.updated')}</Button>}>
            {ov === null ? null : !enabled ? (
              <Space direction="vertical" size={4}>
                <Tag>{t('storage.disabled')}</Tag>
                <span style={{ color: '#999', fontSize: 12 }}>{t('storage.disabledTip')}</span>
              </Space>
            ) : (
              <div>
                <Space wrap>
                  <Tag color={ok ? 'success' : 'error'}>{ok ? t('storage.enabled') : t('storage.disabled')}</Tag>
                  <span style={{ fontSize: 12, color: '#888' }}>
                    {t('storage.endpoint')}: <code>{ov.endpoint || '-'}</code>
                  </span>
                  <span style={{ fontSize: 12, color: '#888' }}>
                    {t('storage.bucket')}: <code>{ov.bucket || '-'}</code>
                  </span>
                </Space>
                {!ok && ov.detail && (
                  <p style={{ color: '#cf1322', fontSize: 12, marginBottom: 8 }}>{ov.detail}</p>
                )}
                <Row gutter={12} style={{ marginTop: 4 }}>
                  <Col span={8}>
                    <div style={{ fontSize: 12, color: '#888' }}>{t('storage.objCount')}</div>
                    <div style={{ fontSize: 16, fontWeight: 600 }}>{ov.objects ?? '-'}</div>
                  </Col>
                  <Col span={8}>
                    <div style={{ fontSize: 12, color: '#888' }}>{t('storage.totalBytes')}</div>
                    <div style={{ fontSize: 16, fontWeight: 600 }}>{fmtBytes(ov.bytes)}</div>
                  </Col>
                  <Col span={8}>
                    <div style={{ fontSize: 12, color: '#888' }}>{t('storage.disk')}</div>
                    <div style={{ fontSize: 16, fontWeight: 600 }}>{diskPct}%</div>
                  </Col>
                </Row>
                <Progress percent={diskPct} size="small"
                  format={() => `${fmtBytes(ov.disk_used)} / ${fmtBytes(ov.disk_total)}`} />
              </div>
            )}
          </Card>
        </Col>
        <Col span={14}>
          <Card size="small" title={<Space><HddOutlined />{t('storage.browser')}</Space>}
            extra={<Select size="small" value={backendId} style={{ width: 200 }} onChange={(v) => { setBackendId(v); setPrefix('') }}
              options={[
                { value: 0, label: t('storage.primaryObject') },
                ...objectBackends.map((b) => ({ value: b.id, label: b.name })),
              ]} />}>
            <Space style={{ marginBottom: 8 }} wrap>
              <Button size="small" icon={<HomeOutlined />} onClick={() => setPrefix('')}>{t('storage.goRoot')}</Button>
              {parts.length > 0 && (
                <Button size="small" icon={<ArrowLeftOutlined />} onClick={goUp}>{t('storage.back')}</Button>
              )}
              {parts.map((p, i) => (
                <Button key={i} size="small" type={i === parts.length - 1 ? 'primary' : 'default'}
                  onClick={() => setPrefix(parts.slice(0, i + 1).join('/') + '/')}>{p}</Button>
              ))}
            </Space>
            <Table size="small" rowKey="name" dataSource={items} pagination={false}
              locale={{ emptyText: t('storage.empty') }}
              columns={[
                {
                  title: t('common.name'), dataIndex: 'name',
                  render: (v: string, r: any) => r.is_dir
                    ? <a onClick={() => setPrefix(v)}>{v.replace(prefix, '').replace(/\/$/, '')} /</a>
                    : <span>{v.replace(prefix, '')}</span>,
                },
                {
                  title: t('storage.colSize'), dataIndex: 'size', width: 110,
                  render: (v: number, r: any) => r.is_dir ? '-' : fmtBytes(v),
                },
                { title: t('storage.colTime'), dataIndex: 'last_modified', width: 190, render: (v: string) => v || '-' },
                {
                  title: t('common.actions'), width: 90,
                  render: (_: any, r: any) => (
                    <Popconfirm title={t('storage.delConfirm')} onConfirm={() => delObjects(r.name)}>
                      <Button size="small" danger icon={<DeleteOutlined />} disabled={!enabled && backendId === 0}>{t('storage.delete')}</Button>
                    </Popconfirm>
                  ),
                },
              ]} />
          </Card>
        </Col>
      </Row>

      <Modal title={t('storage.addBackend')} open={addOpen} onCancel={() => setAddOpen(false)} onOk={addBackend}>
        <Form form={form} layout="vertical" initialValues={{ type: 'object' }}
          onValuesChange={(v) => { if (v.type) setBType(v.type) }}>
          <Form.Item name="name" label={t('common.name')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="type" label={t('storage.colType')} rules={[{ required: true }]}>
            <Select options={['object', 'local', 'nfs', 'block'].map((x) => ({ value: x, label: t(`storage.type${x[0].toUpperCase()}${x.slice(1)}`, { defaultValue: x }) }))} />
          </Form.Item>
          {bType === 'object' && (
            <>
              <Form.Item name="endpoint" label={t('storage.endpoint')} rules={[{ required: true }]}><Input placeholder={t('storage.endpointPh')} /></Form.Item>
              <Form.Item name="bucket" label={t('storage.bucket')} rules={[{ required: true }]}><Input /></Form.Item>
              <Row gutter={8}>
                <Col span={12}><Form.Item name="access_key" label="Access Key"><Input /></Form.Item></Col>
                <Col span={12}><Form.Item name="secret_key" label="Secret Key"><Input.Password /></Form.Item></Col>
              </Row>
            </>
          )}
          {bType !== 'object' && (
            <Form.Item name="path" label={t('storage.pathHost')} rules={[{ required: true }]}><Input placeholder="/mnt/data" /></Form.Item>
          )}
          {bType === 'nfs' && (
            <Form.Item name="server" label="NFS Server" rules={[{ required: true }]}><Input placeholder="192.168.1.10:/export" /></Form.Item>
          )}
          {bType === 'block' && (
            <Form.Item name="device" label={t('storage.device')} rules={[{ required: true }]}><Input placeholder="/dev/vdb" /></Form.Item>
          )}
        </Form>
      </Modal>
    </div>
  )
}

/** 系统管理内旧入口（保留兼容） */
export const StorageTab = StoragePage
