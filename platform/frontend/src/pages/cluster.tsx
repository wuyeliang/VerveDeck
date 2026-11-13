import React, { useEffect, useState } from 'react'
import { Row, Col, Card, Statistic, Table, Tag, Button, Progress, Space } from 'antd'
import { LineChart, Line, XAxis, YAxis, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

export default function ClusterOps() {
  const { t, i18n: ti18n } = useTranslation()
  const [nodes, setNodes] = useState<any[]>([])
  const [metrics, setMetrics] = useState<any[]>([])
  const [queue, setQueue] = useState<any>(null)
  const [util, setUtil] = useState<any>(null)

  const load = () => {
    api.get('/api/cluster/nodes').then((r) => {
      setNodes(r.data)
      if (r.data[0]) {
        api.get(`/api/cluster/nodes/${r.data[0].id}/metrics?minutes=60`)
          .then((m) => {
            const ser = m.data.series?.[0] || []
            setMetrics(ser.map((x: any) => ({ t: new Date(x.ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }), util: x.util, mem: Math.round(x.mem_used / 1024 * 10) / 10, power: Math.round(x.power), temp: x.temp })))
          }).catch(() => {})
      }
    }).catch(() => {})
    api.get('/api/cluster/queue').then((r) => setQueue(r.data)).catch(() => {})
    api.get('/api/cluster/utilization').then((r) => setUtil(r.data)).catch(() => {})
  }
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [])

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('cluster.title')}</h3>
      <Row gutter={16}>
        {nodes.map((n) => (
          <Col span={8} key={n.id}>
            <Card size="small" title={<Space><span>{n.name}</span><Tag color={n.status === 'online' ? 'success' : 'error'}>{n.status}</Tag></Space>}
              extra={<span style={{ color: '#888', fontSize: 12 }}>{t('cluster.heartbeat', { ts: n.last_heartbeat ? n.last_heartbeat.replace('T', ' ').slice(0, 19) : '-' })}</span>}>
              <Row gutter={8}>
                <Col span={8}><Statistic title={t('cluster.gpuTotal')} value={n.gpu_count} /></Col>
                <Col span={8}><Statistic title={t('cluster.gpuUsed')} value={n.gpu_in_use} valueStyle={{ color: '#cf1322' }} /></Col>
                <Col span={8}><Statistic title={t('cluster.gpuFree')} value={Math.max(0, n.gpu_count - n.gpu_in_use)} valueStyle={{ color: '#3f8600' }} /></Col>
              </Row>
              {(n.gpus || []).map((g: any) => (
                <div key={g.index} style={{ marginTop: 8 }}>
                  <div style={{ fontSize: 12, color: '#666' }}>{t('cluster.gpuLine', { idx: g.index, util: g.util, u: (g.mem_used / 1024).toFixed(1), t: (g.mem_total / 1024).toFixed(0), p: g.power, c: g.temp })}</div>
                  <Progress percent={g.util} size="small" showInfo={false} />
                </div>
              ))}
            </Card>
          </Col>
        ))}
        <Col span={24}>
          <Card size="small" title={t('cluster.trend')}>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={metrics}>
                <XAxis dataKey="t" fontSize={11} />
                <YAxis yAxisId="l" domain={[0, 100]} fontSize={11} />
                <YAxis yAxisId="r" orientation="right" fontSize={11} />
                <RTooltip /><Legend />
                <Line yAxisId="l" dataKey="util" name={t('cluster.chartUtil')} stroke="#1677ff" dot={false} />
                <Line yAxisId="r" dataKey="mem" name={t('cluster.chartMem')} stroke="#52c41a" dot={false} />
                <Line yAxisId="r" dataKey="power" name={t('cluster.chartPower')} stroke="#fa8c16" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </Card>
        </Col>
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <Card size="small" title={t('cluster.queueTitle')}>
            <Table size="small" rowKey="id" dataSource={queue?.queued || []} pagination={false} locale={{ emptyText: t('cluster.emptyQueue') }}
              columns={[
                { title: t('common.id'), dataIndex: 'id', width: 55 },
                { title: t('cluster.colTask'), dataIndex: 'name' },
                { title: t('cluster.colStage'), dataIndex: 'stage', width: 70 },
                { title: t('train.dGpu'), dataIndex: 'gpu_count', width: 60 },
                { title: t('cluster.colPriority'), dataIndex: 'priority', width: 70, render: (v: number) => <Tag color={v >= 7 ? 'red' : v >= 4 ? 'blue' : 'default'}>{v}</Tag> },
                {
                  title: t('common.actions'), width: 130, render: (_: any, r: any) => (
                    <Space size={4}>
                      <Button size="small" onClick={() => api.put(`/api/cluster/queue/${r.id}`, { priority: Math.min(9, r.priority + 1) }).then(load)}>+1</Button>
                      <Button size="small" onClick={() => api.put(`/api/cluster/queue/${r.id}`, { priority: Math.max(1, r.priority - 1) }).then(load)}>-1</Button>
                      <Button size="small" type="primary" onClick={() => api.post(`/api/cluster/queue/${r.id}/promote`).then(() => load())}>{t('cluster.top')}</Button>
                    </Space>
                  ),
                },
              ]} />
          </Card>
        </Col>
        <Col span={12}>
          <Card size="small" title={t('cluster.runningTitle')}>
            <Table size="small" rowKey="id" dataSource={queue?.running || []} pagination={false} locale={{ emptyText: t('cluster.emptyRunning') }}
              columns={[
                { title: t('common.id'), dataIndex: 'id', width: 55 },
                { title: t('cluster.colTask'), dataIndex: 'name' },
                { title: t('cluster.colStage'), dataIndex: 'stage', width: 70 },
                { title: t('train.dGpu'), dataIndex: 'gpu_count', width: 60 },
                { title: t('cluster.colStart'), dataIndex: 'started_at', ellipsis: true },
              ]} />
            {util && (
              <p style={{ marginTop: 12, color: '#888' }}>
                {t('cluster.nodeInfo', { node: util.node, d: util.running_deployments, j: util.running_jobs })}
              </p>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
