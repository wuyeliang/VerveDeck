import React, { useEffect, useState } from 'react'
import { Row, Col, Card, Tag, Table, Button, Space, Progress, Popconfirm, Tooltip, message } from 'antd'
import { DeleteOutlined, HomeOutlined, ReloadOutlined, ArrowLeftOutlined } from '@ant-design/icons'
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

const OP_COLOR: Record<string, string> = { put: 'blue', get: 'green', delete: 'red' }

/** 对象存储：对接状态 + 对象浏览 + 同步队列（系统管理 tab 内容） */
export default function StorageTab() {
  const { t } = useTranslation()
  const [ov, setOv] = useState<any>(null)
  const [prefix, setPrefix] = useState('')
  const [items, setItems] = useState<any[]>([])
  const [queue, setQueue] = useState<any>({ items: [] })

  const load = () => {
    api.get('/api/storage').then((r) => setOv(r.data)).catch(() => {})
    api.get('/api/storage/objects', { params: { prefix } }).then((r) => setItems(r.data.items || [])).catch(() => setItems([]))
    api.get('/api/storage/queue', { params: { limit: 50 } }).then((r) => setQueue(r.data)).catch(() => {})
  }
  // prefix 变化重新拉取；其余 10s 轮询（对接状态本身即连通性检查）
  useEffect(() => { load(); const x = setInterval(load, 10000); return () => clearInterval(x) }, [prefix])

  const enabled = !!ov?.enabled
  const ok = ov?.ok !== false
  const diskPct = ov?.disk_total ? Math.round(((ov.disk_used || 0) / ov.disk_total) * 100) : 0
  const parts = prefix ? prefix.split('/').filter(Boolean) : []
  const goUp = () => {
    if (parts.length <= 1) setPrefix('')
    else setPrefix(parts.slice(0, -1).join('/') + '/')
  }

  const delObjects = async (p: string) => {
    try {
      const r = await api.delete('/api/storage/objects', { params: { prefix: p } })
      message.success(`${t('storage.delOk')}: ${r.data.deleted}`)
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('storage.delFail'))
    }
  }
  const retry = async (id: number) => {
    try {
      await api.post(`/api/storage/queue/${id}/retry`)
      message.success(t('storage.retriedOk'))
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('storage.retriedFail'))
    }
  }
  const clearQueue = async (status: string) => {
    try {
      const r = await api.delete('/api/storage/queue', { params: { status } })
      message.success(`${t('storage.updated')}: ${r.data.deleted}`)
      load()
    } catch {
      message.error(t('storage.delFail'))
    }
  }

  return (
    <div>
      <Row gutter={16}>
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
          <Card size="small" title={t('storage.queue')}
            extra={<Space>
              <Button size="small" onClick={() => clearQueue('done')}>{t('storage.clearDone')}</Button>
              <Button size="small" danger onClick={() => clearQueue('failed')}>{t('storage.clearFail')}</Button>
            </Space>}>
            <Table size="small" rowKey="id" dataSource={queue.items || []} pagination={false}
              locale={{ emptyText: t('storage.queueEmpty') }}
              columns={[
                {
                  title: t('storage.colOp'), dataIndex: 'op', width: 70,
                  render: (v: string) => <Tag color={OP_COLOR[v] || 'default'}>{t(`storage.op${v[0]?.toUpperCase()}${v.slice(1)}`, { defaultValue: v })}</Tag>,
                },
                { title: t('storage.colKey'), dataIndex: 'key', ellipsis: true },
                {
                  title: t('storage.colStatus'), dataIndex: 'status', width: 80,
                  render: (v: string) => v === 'done' ? <Tag color="success">{t('storage.stDone')}</Tag> : <Tag color="error">{t('storage.stFailed')}</Tag>,
                },
                {
                  title: t('storage.colError'), dataIndex: 'error', width: 150,
                  render: (v: string) => v ? <Tooltip title={v}><span style={{ color: '#cf1322', fontSize: 12 }}>{v.slice(0, 40)}{v.length > 40 ? '…' : ''}</span></Tooltip> : <span style={{ color: '#ccc' }}>-</span>,
                },
                { title: t('storage.colCreated'), dataIndex: 'created_at', width: 150, render: (v: string) => (v || '').replace('T', ' ').slice(0, 19) },
                {
                  title: t('common.actions'), width: 70,
                  render: (_: any, r: any) => r.status === 'failed'
                    ? <Button size="small" type="link" onClick={() => retry(r.id)}>{t('storage.retry')}</Button>
                    : null,
                },
              ]} />
          </Card>
        </Col>
      </Row>

      <Card size="small" title={t('storage.browser')} style={{ marginTop: 16 }}>
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
                  <Button size="small" danger icon={<DeleteOutlined />} disabled={!enabled}>{t('storage.delete')}</Button>
                </Popconfirm>
              ),
            },
          ]} />
      </Card>
    </div>
  )
}
