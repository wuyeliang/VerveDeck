import React, { useEffect, useState } from 'react'
import { Row, Col, Card, Statistic, Table, Tag, Button, Progress, Space, Tabs, Segmented } from 'antd'
import { LineChart, Line, XAxis, YAxis, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const GPU_COLORS = ['#1677ff', '#52c41a', '#fa8c16', '#722ed1', '#13c2c2', '#eb2f96', '#a0d911', '#f5222d', '#2f54eb', '#faad14']

const THR_KEYS = ['thermal_slowdown', 'hw_slowdown', 'hw_thermal_slowdown', 'hw_power_brake_slowdown', 'sw_thermal_slowdown']
const thrCount = (ex: any) => THR_KEYS.filter((k) => Number(ex[k]) > 0).length
const healthTag = (health: number, t: any) => (
  <Tag color={health >= 2 ? 'error' : health === 1 ? 'warning' : 'success'}
    style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: '16px', padding: '0 5px' }}>
    {health >= 2 ? t('cluster.healthErr') : health === 1 ? t('cluster.healthWarn') : t('cluster.healthOk')}
  </Tag>
)

export default function ClusterOps({ tab = 'nodes' }: { tab?: 'nodes' | 'gpus' | 'monitor' }) {
  const nav = useNavigate()
  const { t, i18n: ti18n } = useTranslation()
  const [nodes, setNodes] = useState<any[]>([])
  const [metrics, setMetrics] = useState<any[]>([])
  const [gpuIdxs, setGpuIdxs] = useState<number[]>([])
  const [queue, setQueue] = useState<any>(null)
  const [util, setUtil] = useState<any>(null)
  const [system, setSystem] = useState<any>(null)
  const [infer, setInfer] = useState<any[]>([])
  const [alerts, setAlerts] = useState<any[]>([])
  const [mins, setMins] = useState(60)

  const load = () => {
    api.get('/api/cluster/nodes').then((r) => {
      setNodes(r.data)
      if (r.data[0]) {
        api.get(`/api/cluster/nodes/${r.data[0].id}/metrics?minutes=${mins}`)
          .then((m) => {
            // series = {gpu_index: [{ts, util, ...}]} → 按 ts 合并为多卡多线数据
            const ser = m.data.series || {}
            const idxs = Object.keys(ser).map(Number).sort((a, b) => a - b)
            const byTs: Record<number, any> = {}
            idxs.forEach((idx) => (ser[idx] || []).forEach((x: any) => {
              const row = byTs[x.ts] || (byTs[x.ts] = { ts: x.ts, t: new Date(x.ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }) })
              row['g' + idx] = x.util
            }))
            setMetrics(Object.values(byTs).sort((a, b) => a.ts - b.ts))
            setGpuIdxs(idxs)
          }).catch(() => {})
      }
    }).catch(() => {})
    api.get('/api/cluster/queue').then((r) => setQueue(r.data)).catch(() => {})
    api.get('/api/cluster/utilization').then((r) => setUtil(r.data)).catch(() => {})
    api.get('/api/cluster/system?minutes=60').then((r) => setSystem(r.data)).catch(() => {})
    api.get('/api/cluster/inference').then((r) => setInfer(r.data.items || [])).catch(() => {})
    api.get('/api/admin/alerts/events', { params: { limit: 10 } }).then((r) => setAlerts(r.data.items || [])).catch(() => {})
  }
  useEffect(() => { load(); const x = setInterval(load, 5000); return () => clearInterval(x) }, [mins])

  const gpus = nodes.flatMap((n) => (n.gpus || []).map((g: any) => ({ ...g, node: n.name })))

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('cluster.title')}</h3>
      <Tabs activeKey={tab} onChange={(k) => nav(`/cluster/${k}`)} items={[
        { key: 'nodes', label: t('cluster.tabNodes'), children: <NodesView nodes={nodes} queue={queue} load={load} /> },
        { key: 'gpus', label: t('cluster.tabGpus'), children: <GpusView gpus={gpus} /> },
        { key: 'monitor', label: t('cluster.tabMonitor'), children: <MonitorView metrics={metrics} gpuIdxs={gpuIdxs} system={system} queue={queue} util={util} infer={infer} alerts={alerts} mins={mins} setMins={setMins} t={t} /> },
      ]} />
    </div>
  )
}

/** 宿主 Linux 系统指标：CPU/内存/磁盘/负载/网络 实时值 + 近 1 小时趋势 */
function SystemCard({ system }: { system: any }) {
  const { t, i18n: ti18n } = useTranslation()
  const last = system?.latest
  const data = (system?.series || []).map((x: any) => ({
    t: new Date(x.ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }),
    cpu: x.cpu, mem: x.mem, disk: x.disk,
  }))
  return (
    <Card size="small" title={t('cluster.systemTitle')} style={{ marginBottom: 16 }}>
      {!last ? (
        <span style={{ color: '#999' }}>{t('cluster.noSysData')}</span>
      ) : (
        <>
          <Row gutter={16}>
            <Col span={4}><Statistic title={t('cluster.sysCpu')} value={last.cpu_pct} suffix="%" precision={0} valueStyle={{ color: last.cpu_pct > 90 ? '#cf1322' : undefined }} /></Col>
            <Col span={4}><Statistic title={t('cluster.sysMem')} value={last.mem_pct} suffix="%" precision={0} valueStyle={{ color: last.mem_pct > 90 ? '#cf1322' : undefined }} /></Col>
            <Col span={4}><Statistic title={t('cluster.sysDisk')} value={last.disk_pct} suffix="%" precision={0} valueStyle={{ color: last.disk_pct > 85 ? '#cf1322' : undefined }} /></Col>
            <Col span={4}><Statistic title={t('cluster.sysLoad')} value={last.load1} precision={2} /></Col>
            <Col span={4}><Statistic title={t('cluster.sysNetRx')} value={Math.round((last.net_rx_mbps || 0) / 8 * 10) / 10} suffix="MB/s" /></Col>
            <Col span={4}><Statistic title={t('cluster.sysNetTx')} value={Math.round((last.net_tx_mbps || 0) / 8 * 10) / 10} suffix="MB/s" /></Col>
          </Row>
          <div style={{ marginTop: 8, color: '#888', fontSize: 12 }}>
            {t('cluster.sysMemDetail', { u: (last.mem_used_mb / 1024).toFixed(1), tgb: (last.mem_total_mb / 1024).toFixed(1) })}
            {' · '}{t('cluster.sysDiskDetail', { u: (last.disk_used_mb / 1024 / 1024).toFixed(1), tg: (last.disk_total_mb / 1024 / 1024).toFixed(1) })}
            {' · '}{t('cluster.sysCpuCores', { n: last.cpu_count })}
          </div>
          <div style={{ marginTop: 12 }}>
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={data}>
                <XAxis dataKey="t" fontSize={11} />
                <YAxis domain={[0, 100]} fontSize={11} />
                <RTooltip /><Legend />
                <Line dataKey="cpu" name={t('cluster.sysCpu')} stroke="#1677ff" dot={false} />
                <Line dataKey="mem" name={t('cluster.sysMem')} stroke="#52c41a" dot={false} />
                <Line dataKey="disk" name={t('cluster.sysDisk')} stroke="#fa8c16" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </Card>
  )
}

/** 计算节点：节点卡（含逐卡概览）+ 队列 + 运行中 */
function NodesView({ nodes, queue, load }: { nodes: any[]; queue: any; load: () => void }) {
  const { t } = useTranslation()
  return (
    <div>
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
              {(n.gpus || []).map((g: any) => {
                const ex = g.extra || {}
                const health = Number(ex.health) || 0
                return (
                  <div key={g.index} style={{ marginTop: 8 }}>
                    <div style={{ fontSize: 12, color: '#666' }}>{g.name ? `${g.name} · ` : ''}{t('cluster.gpuLine', { idx: g.index, util: g.util, u: (g.mem_used / 1024).toFixed(1), t: (g.mem_total / 1024).toFixed(0), p: g.power, c: g.temp })}</div>
                    <Progress percent={g.util} size="small" showInfo={false} />
                    <div style={{ fontSize: 11, color: '#999', marginTop: 2, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {t('cluster.gpuDetail', {
                          mt: ex.mem_temp ? Math.round(ex.mem_temp) : '-',
                          sm: Math.round(ex.sm_clock || 0), mc: Math.round(ex.mem_clock || 0),
                          mu: Math.round(ex.mem_util || 0),
                          ecc: `${Math.round(ex.ecc_dbe_agg || 0)}/${Math.round(ex.ecc_sbe_agg || 0)}`,
                          xid: Math.round(ex.xid_error_count || 0), thr: thrCount(ex),
                        })}
                      </span>
                      {healthTag(health, t)}
                    </div>
                  </div>
                )
              })}
            </Card>
          </Col>
        ))}
      </Row>

      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <Card size="small" title={t('cluster.queueTitle')}>
            <QueueTable queue={queue} load={load} t={t} />
          </Card>
        </Col>
        <Col span={12}>
          <RunningTable queue={queue} t={t} />
        </Col>
      </Row>
    </div>
  )
}

function QueueTable({ queue, load, t }: { queue: any; load: () => void; t: any }) {
  return (
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
  )
}

function RunningTable({ queue, t, util }: { queue: any; t: any; util?: any }) {
  return (
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
  )
}

/** GPU 设备：全集群 GPU 明细表（含 DCGM 指标） */
function GpusView({ gpus }: { gpus: any[] }) {
  const { t } = useTranslation()
  return (
    <Table size="small" rowKey={(r) => `${r.node}-${r.index}`} dataSource={gpus} pagination={false}
      columns={[
        { title: t('cluster.colNode'), dataIndex: 'node', width: 110 },
        { title: t('cluster.colIdx'), dataIndex: 'index', width: 60 },
        { title: t('cluster.colName'), dataIndex: 'name', width: 170, render: (v: string) => v || '-' },
        { title: t('cluster.chartUtil'), dataIndex: 'util', width: 140, render: (v: number) => <Progress percent={v} size="small" /> },
        { title: t('cluster.chartMem'), width: 130, render: (_: any, r: any) => `${(r.mem_used / 1024).toFixed(1)} / ${(r.mem_total / 1024).toFixed(0)} GB` },
        { title: t('cluster.chartPower'), dataIndex: 'power', width: 90, render: (v: number) => `${v} W` },
        { title: t('cluster.colTemp'), dataIndex: 'temp', width: 90, render: (v: number) => `${v} °C` },
        { title: t('cluster.colMemTemp'), width: 90, render: (_: any, r: any) => r.extra?.mem_temp ? `${Math.round(r.extra.mem_temp)} °C` : '-' },
        { title: t('cluster.colClocks'), width: 130, render: (_: any, r: any) => `${Math.round(r.extra?.sm_clock || 0)} / ${Math.round(r.extra?.mem_clock || 0)} MHz` },
        { title: t('cluster.colEcc'), width: 90, render: (_: any, r: any) => `${Math.round(r.extra?.ecc_dbe_agg || 0)} / ${Math.round(r.extra?.ecc_sbe_agg || 0)}` },
        { title: t('cluster.colXid'), width: 70, render: (_: any, r: any) => Math.round(r.extra?.xid_error_count || 0) },
        { title: t('cluster.colThr'), width: 70, render: (_: any, r: any) => thrCount(r.extra || {}) },
        { title: t('cluster.colHealth'), width: 80, render: (_: any, r: any) => healthTag(Number(r.extra?.health) || 0, t) },
      ]} />
  )
}

/** 监控大屏：宿主系统指标 + GPU 逐卡趋势（时间范围可选）+ 推理服务指标 + 最近告警 + 运行中 */
function MonitorView({ metrics, gpuIdxs, system, queue, util, infer, alerts, mins, setMins, t }: {
  metrics: any[]; gpuIdxs: number[]; system: any; queue: any; util: any; infer: any[]; alerts: any[];
  mins: number; setMins: (v: number) => void; t: (k: string) => string,
}) {
  const nav = useNavigate()
  return (
    <div>
      <SystemCard system={system} />
      <Card size="small" title={t('cluster.trend')}
        extra={<Segmented size="small" value={mins} onChange={(v) => setMins(v as number)}
          options={[
            { label: t('cluster.r1h'), value: 60 },
            { label: t('cluster.r6h'), value: 360 },
            { label: t('cluster.r24h'), value: 1440 },
          ]} />}>
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={metrics}>
            <XAxis dataKey="t" fontSize={11} />
            <YAxis domain={[0, 100]} fontSize={11} />
            <RTooltip /><Legend />
            {gpuIdxs.map((idx, i) => (
              <Line key={idx} dataKey={'g' + idx} name={`${t('cluster.gpuShort')} ${idx}`}
                stroke={GPU_COLORS[i % GPU_COLORS.length]} dot={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </Card>
      {infer.length > 0 && <InferCard items={infer} t={t} />}
      <Row gutter={16} style={{ marginTop: 16 }}>
        <Col span={12}>
          <RunningTable queue={queue} t={t} util={util} />
        </Col>
        <Col span={12}>
          <Card size="small" title={t('cluster.recentAlerts')}
            extra={<Button type="link" size="small" onClick={() => nav('/cluster/alerts')}>{t('cluster.viewAll')}</Button>}>
            {alerts.length === 0 ? (
              <span style={{ color: '#999' }}>{t('cluster.noAlerts')}</span>
            ) : (
              <Table size="small" rowKey="id" dataSource={alerts} pagination={false}
                columns={[
                  { title: t('admin.colTime'), width: 140, render: (_: any, r: any) => new Date(r.ts * 1000).toLocaleString() },
                  { title: t('admin.colLevel'), width: 80, render: (_: any, r: any) => levelTag(r.level, t) },
                  { title: t('admin.colTarget'), width: 90, dataIndex: 'target', ellipsis: true },
                  { title: t('admin.colTitle'), dataIndex: 'title', ellipsis: true },
                ]} />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}

/** 推理服务指标：KV 缓存 / 运行中·排队请求 / 延迟 + 近 1h KV 趋势 */
function InferCard({ items, t }: { items: any[]; t: (k: string) => string }) {
  const { i18n: ti18n } = useTranslation()
  return (
    <Card size="small" title={t('cluster.inferTitle')} style={{ marginTop: 16 }}>
      {items.map((d) => (
        <div key={d.id} style={{ marginBottom: 14 }}>
          <Space wrap style={{ marginBottom: 4 }}>
            <b>{d.name}</b><Tag>{d.engine}</Tag>
            {d.latest ? (
              <>
                <span>{t('cluster.kvLabel')}: <b style={{ color: (d.latest.kv || 0) > 90 ? '#cf1322' : undefined }}>{(d.latest.kv || 0).toFixed(0)}%</b></span>
                <span>{t('cluster.runningReq')}: {d.latest.running}</span>
                <span>{t('cluster.waitingReq')}: {d.latest.waiting}</span>
                <span>{t('cluster.latency')}: {Math.round(d.latest.latency_ms)} ms</span>
              </>
            ) : <span style={{ color: '#999' }}>{t('cluster.noInferData')}</span>}
          </Space>
          {(d.series || []).length > 1 && (
            <ResponsiveContainer width="100%" height={80}>
              <LineChart data={d.series.map((x: any) => ({
                t: new Date(x.ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }),
                kv: x.kv,
              }))}>
                <XAxis dataKey="t" fontSize={10} />
                <YAxis domain={[0, 100]} fontSize={10} width={30} />
                <RTooltip />
                <Line dataKey="kv" name={t('cluster.kvLabel')} stroke="#722ed1" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      ))}
    </Card>
  )
}

const levelTag = (v: string, t: (k: string) => string) =>
  v === 'critical' ? <Tag color="error">{t('admin.sevCritical')}</Tag>
    : v === 'warning' ? <Tag color="warning">{t('admin.sevWarning')}</Tag>
      : <Tag>{t('admin.sevInfo')}</Tag>
