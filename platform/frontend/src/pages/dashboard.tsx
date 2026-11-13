import React, { useEffect, useState } from 'react'
import { Row, Col, Card, Statistic, Table, Tag, Progress } from 'antd'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const STATUS_COLOR: Record<string, string> = {
  running: 'processing', queued: 'default', succeeded: 'success', failed: 'error',
  cancelled: 'warning', creating: 'processing', stopped: 'default', pending: 'default',
}

export default function Dashboard() {
  const { t, i18n: ti18n } = useTranslation()
  const [s, setS] = useState<any>(null)
  const [metrics, setMetrics] = useState<any[]>([])

  const load = () => {
    api.get('/api/dashboard/summary').then((r) => setS(r.data)).catch(() => {})
    api.get('/api/cluster/nodes').then((r) => {
      const n = r.data[0]
      if (n) api.get(`/api/cluster/nodes/${n.id}/metrics?minutes=60`)
        .then((m) => {
          const ser = m.data.series?.[0] || []
          setMetrics(ser.map((x: any) => ({ t: new Date(x.ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }), util: x.util, mem: Math.round((x.mem_used / 1024) * 10) / 10 })))
        }).catch(() => {})
    }).catch(() => {})
  }
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [])

  if (!s) return <div>{t('common.loading')}</div>
  return (
    <div>
      <Row gutter={16}>
        <Col span={6}><Card><Statistic title={t('dashboard.gpuUtil')} value={s.gpu.util} suffix="%"
          prefix={<span style={{ color: s.gpu.status === 'online' ? '#3f8600' : '#cf1322', fontSize: 12 }}>● {s.gpu.node} ({s.gpu.status})</span>} /></Card></Col>
        <Col span={6}><Card><Statistic title={t('dashboard.gpuMem')} value={(s.gpu.mem_used / 1024).toFixed(1)} suffix={`/ ${(s.gpu.mem_total / 1024).toFixed(0)}`} /></Card></Col>
        <Col span={4}><Card><Statistic title={t('dashboard.runningJobs')} value={s.jobs_running} /></Card></Col>
        <Col span={4}><Card><Statistic title={t('dashboard.dsVersions')} value={`${s.datasets} / ${s.dataset_versions}`} /></Card></Col>
        <Col span={5}><Card><Statistic title={t('dashboard.modelDeploy')} value={`${s.models} / ${s.deployments_running}`} /></Card></Col>
      </Row>
      <Card title={t('dashboard.gpuTrend')} style={{ marginTop: 16 }} size="small">
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={metrics}>
            <XAxis dataKey="t" fontSize={11} />
            <YAxis yAxisId="l" domain={[0, 100]} fontSize={11} />
            <YAxis yAxisId="r" orientation="right" fontSize={11} unit="GB" />
            <Tooltip />
            <Line yAxisId="l" type="monotone" dataKey="util" name={t('dashboard.chartUtil')} stroke="#1677ff" dot={false} />
            <Line yAxisId="r" type="monotone" dataKey="mem" name={t('dashboard.chartMem')} stroke="#52c41a" dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </Card>
      <Card title={t('dashboard.recentJobs')} style={{ marginTop: 16 }} size="small">
        <Table size="small" rowKey="id" pagination={false} dataSource={s.recent_jobs}
          columns={[
            { title: t('common.id'), dataIndex: 'id', width: 60 },
            { title: t('common.name'), dataIndex: 'name' },
            { title: t('dashboard.stage'), dataIndex: 'stage', width: 80 },
            { title: t('common.status'), dataIndex: 'status', width: 110, render: (v: string) => <Tag color={STATUS_COLOR[v] || 'default'}>{v}</Tag> },
            { title: t('common.createTime'), dataIndex: 'created_at', width: 180 },
          ]} />
      </Card>
    </div>
  )
}
