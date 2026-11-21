import React, { useEffect, useState } from 'react'
import { Row, Col, Card, Statistic, Table, Tag, Progress, Alert, Descriptions, Empty, Divider } from 'antd'
import { LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useStatusLabel } from '../status'

const STATUS_COLOR: Record<string, string> = {
  running: 'processing', queued: 'default', succeeded: 'success', failed: 'error',
  cancelled: 'warning', creating: 'processing', stopped: 'default', pending: 'default',
}

/** 多节点趋势线配色（按节点顺序取模） */
const TREND_PALETTE = ['#1677ff', '#52c41a', '#fa8c16', '#722ed1', '#13c2c2', '#eb2f96']

const fmtBytes = (n: number) => {
  if (!n || n <= 0) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let i = 0, v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i < 2 ? 0 : 1)} ${u[i]}`
}

export default function Dashboard() {
  const { t, i18n: ti18n } = useTranslation()
  const st = useStatusLabel()
  const [s, setS] = useState<any>(null)
  const [metrics, setMetrics] = useState<any[]>([])
  const [trendNodes, setTrendNodes] = useState<string[]>([])

  const load = () => {
    api.get('/api/dashboard/summary').then((r) => setS(r.data)).catch(() => {})
    // 全节点 GPU 趋势：每节点一条利用率线 + 一条显存线（分钟粒度对齐合并）
    api.get('/api/cluster/nodes').then((r) => {
      const ns = r.data || []
      if (!ns.length) return
      Promise.all(ns.map((n) =>
        api.get(`/api/cluster/nodes/${n.id}/metrics?minutes=60`)
          .then((m) => ({ name: m.data.node || n.name, series: m.data.series || {} }))
          .catch(() => null),
      )).then((rs) => {
        const perNode: { name: string; pts: [number, number, number][] }[] = []
        for (const r of rs) {
          if (!r) continue
          const byMin = new Map<number, { u: number[]; m: number[] }>()
          for (const pts of Object.values(r.series) as any[]) {
            for (const p of pts || []) {
              const k = Math.floor((p.ts || 0) / 60) * 60
              const e = byMin.get(k) || { u: [], m: [] }
              e.u.push(p.util ?? 0); e.m.push(p.mem_used ?? 0)
              byMin.set(k, e)
            }
          }
          const pts = [...byMin.entries()].sort((a, b) => a[0] - b[0])
            .map(([ts, e]): [number, number, number] => [ts,
              Math.round(e.u.reduce((x, y) => x + y, 0) / e.u.length),
              Math.round(e.m.reduce((x, y) => x + y, 0) / 1024 * 10) / 10])
          if (pts.length) perNode.push({ name: r.name, pts })
        }
        const rows: any[] = []
        const rowIdx = new Map<number, number>()
        for (const n of perNode) {
          for (const [ts, u, m] of n.pts) {
            let i = rowIdx.get(ts)
            if (i === undefined) {
              i = rows.length
              rowIdx.set(ts, i)
              rows.push({ t: new Date(ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }) })
            }
            rows[i][`u:${n.name}`] = u
            rows[i][`m:${n.name}`] = m
          }
        }
        setTrendNodes(perNode.map((n) => n.name))
        setMetrics(rows)
      })
    }).catch(() => {})
  }
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [])

  if (!s) return <div>{t('common.loading')}</div>

  const alerts = (s.alerts || []).map((a: any, i: number) => ({
    key: i, type: a.level === 'error' ? 'error' : 'warning',
    message: t(`dashboard.alerts.${a.code}` as string, { arg: a.arg ?? '' }),
  }))

  const healthTag = (h: number) =>
    h >= 2 ? <Tag color="error">{t('cluster.healthErr')}</Tag>
      : h === 1 ? <Tag color="warning">{t('cluster.healthWarn')}</Tag>
      : <Tag color="success">{t('cluster.healthOk')}</Tag>

  const stTag = (v: string) => <Tag color={STATUS_COLOR[v] || 'default'}>{st(v)}</Tag>

  // GPU 汇总标签：多节点时显示节点数（维护中节点不计入）；状态取聚合口径（任一在线即在线）
  const gpuNodes: any[] = (s.gpu && s.gpu.nodes) || []
  const activeGpuNodes = gpuNodes.filter((n: any) => !n.maintenance)
  const gpuNodeLabel = gpuNodes.length <= 1 ? (s.gpu.node || '-') : t('dashboard.multiNodes', { n: activeGpuNodes.length })
  const gpuTotalCount = activeGpuNodes.reduce((sum: number, n: any) => sum + (n.count || 0), 0)
  const gpuSt = gpuNodes.length ? (activeGpuNodes.some((n: any) => n.status === 'online') ? 'online' : 'offline') : 'unknown'

  const diskPct = s.storage?.disk_total ? Math.round((s.storage.disk_used / s.storage.disk_total) * 100) : 0

  return (
    <div>
      {alerts.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          {alerts.map((a) => <Alert key={a.key} type={a.type} showIcon message={a.message} style={{ marginBottom: 8 }} />)}
        </div>
      )}
      {/* 第一行：核心指标 */}
      <Row gutter={16}>
        <Col span={4}><Card size="small"><Statistic title={t('dashboard.gpuUtil')} value={s.gpu.util} suffix="%"
          prefix={<span style={{ color: gpuSt === 'online' ? '#3f8600' : '#cf1322', fontSize: 12 }}>● {gpuNodeLabel} ({st(gpuSt)}) × {gpuTotalCount}</span>} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('dashboard.gpuMem')} value={(s.gpu.mem_used / 1024).toFixed(1)} suffix={`/ ${(s.gpu.mem_total / 1024).toFixed(0)} GB`} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('dashboard.trainCard')} value={s.jobs_running}
          suffix={<span style={{ fontSize: 12, color: '#999' }}>{t('dashboard.running')} / {s.jobs_total}</span>}
          valueStyle={{ color: s.jobs_queued ? '#d48806' : undefined }}
          prefix={s.jobs_queued ? <span style={{ fontSize: 12, color: '#d48806' }}>{s.jobs_queued} {t('dashboard.queued')}</span> : null} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('dashboard.evalCard')} value={s.evals_succeeded}
          suffix={<span style={{ fontSize: 12, color: '#999' }}>{t('dashboard.done')} / {s.evals}</span>}
          prefix={s.evals_running ? <span style={{ fontSize: 12, color: '#1677ff' }}>{s.evals_running} {t('dashboard.running')}</span> : null} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('dashboard.modelCard')} value={s.models}
          suffix={<span style={{ fontSize: 12, color: '#999' }}>{s.deployments_running} {t('nav.infer')}</span>}
          prefix={<span style={{ fontSize: 12, color: '#999' }}>{s.models_by_type?.base ?? 0}/{s.models_by_type?.lora ?? 0}/{s.models_by_type?.merged ?? 0}</span>} /></Card></Col>
        <Col span={4}><Card size="small"><Statistic title={t('dashboard.dsVersions')} value={`${s.datasets} / ${s.dataset_versions}`}
          suffix={<span style={{ fontSize: 12, color: '#999' }}>{s.total_samples ?? 0} {t('dashboard.samplesTotal')}</span>} /></Card></Col>
      </Row>
      {/* 第二行：GPU 实时状态 + 系统状态 */}
      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={14}>
          <Card title={t('dashboard.gpusTitle')} size="small">
            {s.gpus?.length ? (
              <Table size="small" rowKey="index" pagination={false} dataSource={s.gpus}
                columns={[
                  { title: 'GPU', dataIndex: 'index', width: 56 },
                  { title: t('common.name'), dataIndex: 'name', ellipsis: true },
                  { title: t('dashboard.chartUtil'), dataIndex: 'util', width: 140, render: (v: number) => <Progress percent={Math.round(v)} size="small" /> },
                  { title: t('dashboard.memUsed'), width: 110, render: (_: any, r: any) => `${(r.mem_used / 1024).toFixed(1)} / ${(r.mem_total / 1024).toFixed(0)} GB` },
                  { title: t('dashboard.power'), width: 90, render: (_: any, r: any) => `${r.power.toFixed(0)} W` },
                  { title: t('dashboard.temp'), width: 80, render: (_: any, r: any) => `${r.temp.toFixed(0)} °C` },
                  { title: t('dashboard.smClock'), width: 100, render: (_: any, r: any) => `${r.sm_clock.toFixed(0)} MHz` },
                  { title: t('dashboard.health'), width: 90, render: (_: any, r: any) => healthTag(r.health) },
                ]} />
            ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
          </Card>
        </Col>
        <Col span={10}>
          <Card title={t('dashboard.sysTitle')} size="small">
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label={t('nav.cluster')}>
                <Tag color={gpuSt === 'online' ? 'success' : 'error'}>{gpuNodeLabel} · {st(gpuSt)}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t('dashboard.storage')}>
                <Tag color={s.storage?.ok ? 'success' : 'error'}>{s.storage?.ok ? t('common.enabled') : t('common.disabled')}</Tag>
                <span style={{ color: '#999', fontSize: 12 }}>{s.storage?.enabled ? s.storage.bucket : t('dashboard.localStorage')}</span>
              </Descriptions.Item>
              <Descriptions.Item label={t('dashboard.diskUsed')} span={2}>
                <Progress percent={diskPct} size="small"
                  format={() => `${fmtBytes(s.storage?.disk_used || 0)} / ${fmtBytes(s.storage?.disk_total || 0)}`} />
              </Descriptions.Item>
              <Descriptions.Item label={t('dashboard.users')}>{s.system?.users ?? 0}</Descriptions.Item>
              <Descriptions.Item label={t('dashboard.projects')}>{s.system?.projects ?? 0}</Descriptions.Item>
              <Descriptions.Item label={t('dashboard.apiKeys')}>{s.system?.api_keys ?? 0}</Descriptions.Item>
              <Descriptions.Item label={t('dashboard.dataTasks')}>
                <Tag color={(s.data_tasks?.running || 0) ? 'processing' : 'default'}>{s.data_tasks?.done ?? 0} {t('dashboard.done')} / {s.data_tasks?.total ?? 0}</Tag>
              </Descriptions.Item>
            </Descriptions>
            <Divider style={{ margin: '12px 0' }} />
            <Row gutter={12}>
              <Col span={8}><Statistic title={`${t('dashboard.trainCard')} · ${t('dashboard.queued')}`} value={s.jobs_queued} valueStyle={{ fontSize: 20, color: s.jobs_queued ? '#d48806' : undefined }} /></Col>
              <Col span={8}><Statistic title={`${t('dashboard.evalCard')} · ${t('dashboard.running')}`} value={s.evals_running} valueStyle={{ fontSize: 20, color: s.evals_running ? '#1677ff' : undefined }} /></Col>
              <Col span={8}><Statistic title={`${t('dashboard.evalCard')} · ${t('dashboard.failed')}`} value={s.evals_failed} valueStyle={{ fontSize: 20, color: s.evals_failed ? '#cf1322' : undefined }} /></Col>
            </Row>
          </Card>
        </Col>
      </Row>
      {/* 第三行：GPU 趋势（每节点一条利用率实线 + 一条显存虚线；单节点时与旧口径一致） */}
      <Card title={t('dashboard.gpuTrend')} style={{ marginTop: 16 }} size="small">
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={metrics}>
            <XAxis dataKey="t" fontSize={11} />
            <YAxis yAxisId="l" domain={[0, 100]} fontSize={11} />
            <YAxis yAxisId="r" orientation="right" fontSize={11} unit="GB" />
            <Tooltip />
            {trendNodes.length > 1 && <Legend wrapperStyle={{ fontSize: 12 }} />}
            {trendNodes.map((nm, i) => {
              const color = trendNodes.length === 1 ? '#1677ff' : TREND_PALETTE[i % TREND_PALETTE.length]
              const memColor = trendNodes.length === 1 ? '#52c41a' : color
              const prefix = trendNodes.length > 1 ? `${nm} ` : ''
              return (
                <React.Fragment key={nm}>
                  <Line yAxisId="l" type="monotone" dataKey={`u:${nm}`} name={`${prefix}${t('dashboard.chartUtil')}`} stroke={color} dot={false} connectNulls />
                  <Line yAxisId="r" type="monotone" dataKey={`m:${nm}`} name={`${prefix}${t('dashboard.chartMem')}`} stroke={memColor} strokeDasharray={trendNodes.length > 1 ? '5 3' : undefined} dot={false} connectNulls />
                </React.Fragment>
              )
            })}
          </LineChart>
        </ResponsiveContainer>
      </Card>
      {/* 第四行：最近训练 + 最近评测 */}
      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <Card title={t('dashboard.recentJobs')} size="small">
            <Table size="small" rowKey="id" pagination={false} dataSource={s.recent_jobs}
              columns={[
                { title: t('common.id'), dataIndex: 'id', width: 50 },
                { title: t('common.name'), dataIndex: 'name', ellipsis: true },
                { title: t('dashboard.stage'), dataIndex: 'stage', width: 76 },
                { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => stTag(v) },
                { title: t('common.createTime'), dataIndex: 'created_at', width: 165 },
              ]} />
          </Card>
        </Col>
        <Col span={12}>
          <Card title={t('dashboard.recentEvals')} size="small">
            <Table size="small" rowKey="id" pagination={false} dataSource={s.recent_evals}
              columns={[
                { title: t('common.id'), dataIndex: 'id', width: 50 },
                { title: t('common.name'), dataIndex: 'name', ellipsis: true },
                { title: t('dashboard.method'), dataIndex: 'method', width: 80, render: (v: string, r: any) => v === 'benchmark' ? (r.benchmark || v) : v },
                { title: t('dashboard.score'), dataIndex: 'score', width: 70, render: (v: number | null) => v == null ? '-' : v },
                { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => stTag(v) },
                { title: t('common.createTime'), dataIndex: 'created_at', width: 165 },
              ]} />
          </Card>
        </Col>
      </Row>
      {/* 第五行：最近部署 + 最近操作 */}
      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <Card title={t('dashboard.recentDepts')} size="small">
            <Table size="small" rowKey="id" pagination={false} dataSource={s.recent_depts}
              columns={[
                { title: t('common.id'), dataIndex: 'id', width: 50 },
                { title: t('common.name'), dataIndex: 'name', ellipsis: true },
                { title: t('dashboard.model'), dataIndex: 'model', ellipsis: true },
                { title: t('dashboard.engine'), dataIndex: 'engine', width: 70 },
                { title: t('dashboard.replicas'), dataIndex: 'replicas', width: 60 },
                { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => stTag(v) },
              ]} />
          </Card>
        </Col>
        <Col span={12}>
          <Card title={t('dashboard.recentActivity')} size="small">
            <Table size="small" rowKey="id" pagination={false} dataSource={s.recent_audit}
              columns={[
                { title: t('dashboard.timeCol'), dataIndex: 'ts', width: 160, render: (v: number) => new Date(v * 1000).toLocaleString(ti18n.language) },
                { title: t('dashboard.userCol'), dataIndex: 'username', width: 100, render: (v: string) => v || '-' },
                { title: t('dashboard.action'), dataIndex: 'method', width: 64, render: (v: string) => <Tag>{v}</Tag> },
                { title: t('common.detail'), dataIndex: 'path', ellipsis: true },
                { title: t('dashboard.ipCol'), dataIndex: 'ip', width: 120, render: (v: string) => v || '-' },
              ]} />
          </Card>
        </Col>
      </Row>
    </div>
  )
}
