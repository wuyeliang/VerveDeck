import React, { useEffect, useState } from 'react'
import { Row, Col, Card, Statistic, Table, Tag, Button, Progress, Space, Tabs, Segmented } from 'antd'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const healthTag = (health: number, t: any) => (
  <Tag color={health >= 2 ? 'error' : health === 1 ? 'warning' : 'success'}
    style={{ marginInlineEnd: 0, fontSize: 10, lineHeight: '16px', padding: '0 5px' }}>
    {health >= 2 ? t('cluster.healthErr') : health === 1 ? t('cluster.healthWarn') : t('cluster.healthOk')}
  </Tag>
)

/** 网络速率（字节/秒）→ 人类可读 */
const fmtBps = (v: number | null | undefined): string => {
  if (v == null) return '-'
  const kb = v / 1024
  if (kb < 1024) return `${kb.toFixed(1)} KB/s`
  const mb = kb / 1024
  if (mb < 1024) return `${mb.toFixed(1)} MB/s`
  return `${(mb / 1024).toFixed(2)} GB/s`
}

const barPct = (v: number | null | undefined) => (v == null ? 0 : Math.max(0, Math.min(100, Math.round(v))))

/** 节点卡宿主四指标（CPU / 内存 / 磁盘 / 网络收发），数据源 node-exporter */
function HostMetrics({ host, t }: { host: any; t: any }) {
  if (!host) return null
  const pcts = [
    { k: 'cpu', label: t('cluster.sysCpu'), v: host.cpu_pct },
    { k: 'mem', label: t('cluster.sysMem'), v: host.mem_pct },
    { k: 'disk', label: t('cluster.sysDisk'), v: host.disk_pct },
  ]
  const rx = host.net_rx_bps, tx = host.net_tx_bps
  const netStr = (rx == null && tx == null) ? '-' : `${fmtBps(rx)} / ${fmtBps(tx)}`
  return (
    <div style={{ marginTop: 8, background: '#fafafa', borderRadius: 6, padding: '6px 8px 2px' }}>
      <div style={{ fontSize: 11, color: '#bbb', marginBottom: 2 }}>{t('cluster.hostMetrics')}</div>
      <Row gutter={8}>
        {pcts.map((it) => (
          <Col span={6} key={it.k}>
            <div style={{ fontSize: 11, color: '#999' }}>{it.label}</div>
            <Progress percent={barPct(it.v)} size="small"
              status={it.v != null && it.v > 90 ? 'exception' : undefined}
              format={() => <span style={{ fontSize: 11 }}>{it.v == null ? '-' : `${Math.round(it.v)}%`}</span>} />
          </Col>
        ))}
        <Col span={6}>
          <div style={{ fontSize: 11, color: '#999' }}>{t('cluster.sysNet')}</div>
          <div style={{ fontSize: 12, color: '#333', lineHeight: '14px', marginTop: 4,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={netStr}>{netStr}</div>
        </Col>
      </Row>
    </div>
  )
}

export default function ClusterOps({ tab = 'nodes' }: { tab?: 'nodes' | 'gpus' | 'monitor' }) {
  const nav = useNavigate()
  const { t } = useTranslation()
  const [nodes, setNodes] = useState<any[]>([])
  const [queue, setQueue] = useState<any>(null)
  const [util, setUtil] = useState<any>(null)
  const [alerts, setAlerts] = useState<any[]>([])

  const load = () => {
    api.get('/api/cluster/nodes').then((r) => setNodes(r.data)).catch(() => {})
    api.get('/api/cluster/queue').then((r) => setQueue(r.data)).catch(() => {})
    api.get('/api/cluster/utilization').then((r) => setUtil(r.data)).catch(() => {})
    api.get('/api/admin/alerts/events', { params: { limit: 10 } }).then((r) => setAlerts(r.data.items || [])).catch(() => {})
  }
  useEffect(() => { load(); const x = setInterval(load, 5000); return () => clearInterval(x) }, [])

  const gpus = nodes.flatMap((n) => (n.gpus || []).map((g: any) => ({ ...g, node: n.name })))

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('cluster.title')}</h3>
      <Tabs activeKey={tab} onChange={(k) => nav(`/cluster/${k}`)} items={[
        { key: 'nodes', label: t('cluster.tabNodes'), children: <NodesView nodes={nodes} queue={queue} load={load} /> },
        { key: 'gpus', label: t('cluster.tabGpus'), children: <GpusView gpus={gpus} /> },
        { key: 'monitor', label: t('cluster.tabMonitor'), children: <MonitorView queue={queue} util={util} alerts={alerts} t={t} /> },
      ]} />
    </div>
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
              {(n.driver || n.cuda) && (
                <div style={{ fontSize: 11, color: '#999', margin: '6px 0 0' }}>
                  {n.driver ? `${t('cluster.driver')}: ${n.driver}` : ''}
                  {n.driver && n.cuda ? ' · ' : ''}
                  {n.cuda ? `${t('cluster.cuda')}: ${n.cuda}` : ''}
                </div>
              )}
              <HostMetrics host={n.host} t={t} />
              {(n.gpus || []).map((g: any) => (
                <div key={g.index} style={{ marginTop: 8 }}>
                  <div style={{ fontSize: 12, color: '#666' }}>{g.name ? `${g.name} · ` : ''}{t('cluster.gpuLine', { idx: g.index, util: Math.round(g.util || 0), u: (g.mem_used / 1024).toFixed(1), t: (g.mem_total / 1024).toFixed(0), p: Math.round(g.power || 0), c: Math.round(g.temp || 0) })}</div>
                  <Progress percent={Math.round(g.util || 0)} size="small" showInfo={false} />
                  <div style={{ fontSize: 11, color: '#999', marginTop: 2, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {t('cluster.gpuDetail', {
                        mt: g.mem_temp ? Math.round(g.mem_temp) : '-',
                        sm: Math.round(g.sm_clock || 0), mc: Math.round(g.mem_clock || 0),
                        ecc: `${Math.round(g.ecc_dbe || 0)}/${Math.round(g.ecc_sbe || 0)}`,
                        xid: Math.round(g.xid || 0), thr: g.throttle ? 1 : 0,
                      })}
                    </span>
                    {healthTag(g.health || 0, t)}
                  </div>
                </div>
              ))}
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
        { title: t('cluster.chartUtil'), dataIndex: 'util', width: 140, render: (v: number) => <Progress percent={Math.round(v || 0)} size="small" /> },
        { title: t('cluster.chartMem'), width: 170, render: (_: any, r: any) => {
            const pct = r.mem_total ? Math.round((r.mem_used / r.mem_total) * 100) : 0
            return (
              <div>
                <div style={{ fontSize: 12, marginBottom: 2 }}>{(r.mem_used / 1024).toFixed(1)} / {(r.mem_total / 1024).toFixed(0)} GB</div>
                <Progress percent={pct} size="small" showInfo={false} status={pct > 95 ? 'exception' : undefined} />
              </div>
            )
          } },
        { title: t('cluster.chartPower'), dataIndex: 'power', width: 90, render: (v: number) => `${Math.round(v || 0)} W` },
        { title: t('cluster.colPowerLimit'), width: 90, render: (_: any, r: any) => (r.power_limit ? `${Math.round(r.power_limit)} W` : '-') },
        { title: t('cluster.colTemp'), dataIndex: 'temp', width: 90, render: (v: number) => `${Math.round(v || 0)} °C` },
        { title: t('cluster.colMemTemp'), width: 90, render: (_: any, r: any) => r.mem_temp ? `${Math.round(r.mem_temp)} °C` : '-' },
        { title: t('cluster.colClocks'), width: 130, render: (_: any, r: any) => `${Math.round(r.sm_clock || 0)} / ${Math.round(r.mem_clock || 0)} MHz` },
        { title: t('cluster.colEcc'), width: 90, render: (_: any, r: any) => `${Math.round(r.ecc_dbe || 0)} / ${Math.round(r.ecc_sbe || 0)}` },
        { title: t('cluster.colXid'), width: 70, render: (_: any, r: any) => Math.round(r.xid || 0) },
        { title: t('cluster.colRemap'), width: 70, render: (_: any, r: any) => {
            const v = Math.round(r.remap || 0)
            return v > 0 ? <Tag color="error">{v}</Tag> : '0'
          } },
        { title: t('cluster.colThr'), width: 70, render: (_: any, r: any) => (r.throttle ? 1 : 0) },
        { title: t('cluster.colHealth'), width: 80, render: (_: any, r: any) => healthTag(Number(r.health) || 0, t) },
      ]} />
  )
}

/** 监控大屏：Grafana 仪表盘嵌入（GPU / 系统 / 推理，kiosk 只读视图，经 nginx /grafana/ 同源代理）
 *  数据底座 = Prometheus（dcgm-exporter + node-exporter + 引擎 /metrics，保留 15d）；
 *  最近告警 / 运行中任务仍由平台侧展示。 */
function MonitorView({ queue, util, alerts, t }: { queue: any; util: any; alerts: any[]; t: (k: string) => string }) {
  const nav = useNavigate()
  const [mins, setMins] = useState(60)
  const [dash, setDash] = useState('vervedeck-gpu')
  const from = ({ 60: 'now-1h', 360: 'now-6h', 1440: 'now-24h' } as Record<number, string>)[mins]
  const dashboards = [
    { key: 'vervedeck-gpu', label: t('cluster.monGpu') },
    { key: 'vervedeck-node', label: t('cluster.monNode') },
    { key: 'vervedeck-infer', label: t('cluster.monInfer') },
  ]
  return (
    <div>
      <Card size="small" style={{ marginBottom: 16 }}
        title={<Space>{t('cluster.monTitle')}<span style={{ color: '#999', fontSize: 12 }}>{t('cluster.monHint')}</span></Space>}
        extra={
          <Space>
            <Segmented size="small" value={mins} onChange={(v) => setMins(v as number)}
              options={[
                { label: t('cluster.r1h'), value: 60 },
                { label: t('cluster.r6h'), value: 360 },
                { label: t('cluster.r24h'), value: 1440 },
              ]} />
            <Button size="small" onClick={() => window.open(`/grafana/d/${dash}/?orgId=1&from=${from}`, '_blank')}>{t('cluster.monFull')}</Button>
          </Space>
        }>
        <Tabs size="small" activeKey={dash} onChange={setDash}
          items={dashboards.map((d) => ({ key: d.key, label: d.label }))} />
        <iframe key={`${dash}-${mins}`} title={dash}
          src={`/grafana/d/${dash}/?kiosk&orgId=1&from=${from}&to=now`}
          style={{ width: '100%', height: 540, border: '1px solid #f0f0f0', borderRadius: 6, background: '#fff' }} />
      </Card>
      <Row gutter={16}>
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

const levelTag = (v: string, t: (k: string) => string) =>
  v === 'critical' ? <Tag color="error">{t('admin.sevCritical')}</Tag>
    : v === 'warning' ? <Tag color="warning">{t('admin.sevWarning')}</Tag>
      : <Tag>{t('admin.sevInfo')}</Tag>
