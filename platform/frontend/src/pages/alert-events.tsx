// 告警事件页（集群运维 > 告警事件）：事件流（10s 轮询）+ 级别/目标/确认状态筛选 + 确认/取消确认/删除（单条与批量）
import React, { useEffect, useState } from 'react'
import {
  Table, Button, Space, Tag, Modal, Input, Select, message, Popconfirm, Card, Tooltip,
} from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'
import { sevTag } from './alerts-shared'

const EV_PAGE = 200  // 事件流展示条数（服务端 limit）

export default function AlertEventsPage() {
  const { t, i18n: ti18n } = useTranslation()
  const [events, setEvents] = useState<any[]>([])
  const [evLevel, setEvLevel] = useState('')
  const [evTarget, setEvTarget] = useState('')
  const [evAcked, setEvAcked] = useState('')
  const [evSel, setEvSel] = useState<number[]>([])

  const fetchEvents = (lv = evLevel, tg = evTarget, ak = evAcked) =>
    api.get('/api/admin/alerts/events', {
      params: { limit: EV_PAGE, level: lv || undefined, target: tg || undefined, acked: ak || undefined },
    }).then((r) => setEvents(r.data.items || [])).catch(() => {})

  // 筛选变化即重取；10s 轮询经 ref 取最新筛选
  useEffect(() => { fetchEvents() }, [evLevel, evTarget, evAcked])
  const pollRef = React.useRef<any>({})
  pollRef.current = { fetchEvents }
  useEffect(() => {
    const x = setInterval(() => pollRef.current.fetchEvents(), 10000)
    return () => clearInterval(x)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const batchDelEv = useBatchDelete('/api/admin/alerts/events/batch-delete', () => { setEvSel([]); fetchEvents() })

  const ackEvents = (ids: number[], ack: boolean) => {
    if (!ids.length) return
    const doIt = async () => {
      try {
        await api.post('/api/admin/alerts/events/ack', { ids, ack })
        setEvSel([])
        fetchEvents()
      } catch (e: any) { message.error(e?.response?.data?.detail || e?.message) }
    }
    if (ack) doIt()
    else Modal.confirm({ title: t('admin.unackConfirm'), okText: t('admin.unack'), onOk: doIt })
  }

  const ackTag = (e: any) => e.acked
    ? <Tooltip title={`${t('admin.ackedBy')}: ${e.acked_by || '-'}${e.acked_at ? ` · ${e.acked_at}` : ''}`}>
        <Tag color="success">{t('admin.acked')}</Tag>
      </Tooltip>
    : <Tag>{t('admin.unacked')}</Tag>

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('admin.tabAlertEvents')}</h3>
      <Card size="small" title={t('admin.eventsTitle')}
        extra={
          <Space size={6}>
            <Input size="small" allowClear placeholder={t('admin.evSearchPh')} style={{ width: 150 }}
              value={evTarget} onChange={(e) => setEvTarget(e.target.value)} />
            <Select size="small" allowClear placeholder={t('admin.colLevel')} style={{ width: 110 }}
              value={evLevel || undefined} onChange={(v) => setEvLevel(v || '')}
              options={[
                { value: 'critical', label: t('admin.sevCritical') },
                { value: 'warning', label: t('admin.sevWarning') },
                { value: 'info', label: t('admin.sevInfo') },
              ]} />
            <Select size="small" allowClear placeholder={t('common.status')} style={{ width: 120 }}
              value={evAcked || undefined} onChange={(v) => setEvAcked(v || '')}
              options={[
                { value: '0', label: t('admin.unacked') },
                { value: '1', label: t('admin.acked') },
              ]} />
          </Space>
        }>
        <Space size={8} style={{ marginBottom: 8 }}>
          {evSel.length > 0 && <>
            <Button size="small" type="primary" onClick={() => ackEvents(evSel, true)}>{t('admin.batchAck')}（{evSel.length}）</Button>
            <Button size="small" onClick={() => ackEvents(evSel, false)}>{t('admin.batchUnack')}（{evSel.length}）</Button>
            <Button size="small" danger onClick={() => batchDelEv(evSel)}>{t('common.batchDelete')}（{evSel.length}）</Button>
          </>}
        </Space>
        <Table size="small" rowKey="id" dataSource={events}
          rowSelection={{ selectedRowKeys: evSel, onChange: (k: any) => setEvSel(k) }}
          pagination={false} locale={{ emptyText: '-' }}
          columns={[
            { title: t('admin.colTime'), width: 160, render: (_: any, r: any) => new Date(r.ts * 1000).toLocaleString(ti18n.language) },
            { title: t('admin.colLevel'), dataIndex: 'level', width: 90, render: (v: string) => sevTag(t, v) },
            { title: t('admin.colTarget'), dataIndex: 'target', width: 110 },
            { title: t('admin.colTitle'), dataIndex: 'title', ellipsis: true },
            { title: t('admin.colNotified'), dataIndex: 'notified', width: 90, render: (v: boolean) => v ? <Tag color="success">{t('common.yes')}</Tag> : <Tag>{t('common.no')}</Tag> },
            { title: t('common.status'), dataIndex: 'acked', width: 100, render: (_: any, r: any) => ackTag(r) },
            {
              title: t('common.actions'), width: 150, render: (_: any, r: any) => (
                <Space size={4}>
                  <Button size="small" onClick={() => ackEvents([r.id], !r.acked)}>{r.acked ? t('admin.unack') : t('admin.ack')}</Button>
                  <Popconfirm title={t('admin.del')} onConfirm={() => api.post('/api/admin/alerts/events/batch-delete', { ids: [r.id] }).then(() => fetchEvents())}>
                    <Button size="small" danger>{t('common.delete')}</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]} />
      </Card>
    </div>
  )
}
