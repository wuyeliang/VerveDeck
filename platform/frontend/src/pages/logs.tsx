import React, { useCallback, useEffect, useRef, useState } from 'react'
import { Card, Select, Switch, Input, Button, Space, Tag, Table, Tabs, message } from 'antd'
import { CopyOutlined, DownloadOutlined, ReloadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

/** 日志中心（超管左菜单）：审计日志 + 容器运行日志（原系统管理内审计日志已并入本页） */
export default function LogsPage() {
  const { t } = useTranslation()
  const [tab, setTab] = useState('audit')
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('logs.title')}</h3>
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'audit', label: t('logs.tabAudit'), children: <AuditTab /> },
        { key: 'container', label: t('logs.tabContainer'), children: <ContainerTab active={tab === 'container'} /> },
      ]} />
    </div>
  )
}

/** 审计日志（原系统管理-审计 tab 迁入）：/api/admin/audit 五维筛选 */
function AuditTab() {
  const { t, i18n: ti18n } = useTranslation()
  const [data, setData] = useState<any>({ items: [], total: 0 })
  const [page, setPage] = useState(1)
  const [user, setUser] = useState('')
  const [action, setAction] = useState('')
  const [method, setMethod] = useState('')
  const [status, setStatus] = useState('')
  const [pathQ, setPathQ] = useState('')
  const [actions, setActions] = useState<any[]>([])

  useEffect(() => {
    api.get('/api/admin/audit/actions').then((r) => setActions(r.data || [])).catch(() => {})
  }, [])
  const load = useCallback(() => {
    api.get('/api/admin/audit', {
      params: { limit: 20, offset: (page - 1) * 20, username: user, action, method, status, path_q: pathQ },
    }).then((r) => setData(r.data)).catch(() => {})
  }, [page, user, action, method, status, pathQ])
  useEffect(() => { load() }, [load])
  const actLabel = (code: string) => t(`audit.act.${code}`, { defaultValue: code || '-' })
  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input placeholder={t('admin.auditFilter')} value={user} onChange={(e) => { setUser(e.target.value); setPage(1) }} style={{ width: 140 }} />
        <Select allowClear placeholder={t('admin.auditFilterAction')} value={action || undefined}
          onChange={(v) => { setAction(v || ''); setPage(1) }} style={{ width: 170 }}
          options={actions.map((a) => ({ value: a.action, label: `${actLabel(a.action)} (${a.count})` }))} />
        <Select allowClear placeholder={t('admin.auditFilterMethod')} value={method || undefined}
          onChange={(v) => { setMethod(v || ''); setPage(1) }} style={{ width: 110 }}
          options={['GET', 'POST', 'PUT', 'DELETE'].map((x) => ({ value: x, label: x }))} />
        <Select allowClear placeholder={t('admin.auditFilterStatus')} value={status || undefined}
          onChange={(v) => { setStatus(v || ''); setPage(1) }} style={{ width: 100 }}
          options={[{ value: 'ok', label: t('admin.auditOk') }, { value: 'err', label: t('admin.auditErr') }]} />
        <Input placeholder={t('admin.auditFilterPath')} value={pathQ} onChange={(e) => { setPathQ(e.target.value); setPage(1) }} style={{ width: 160 }} />
      </Space>
      <Table size="small" rowKey="id" dataSource={data.items}
        pagination={{ current: page, total: data.total, pageSize: 20, onChange: setPage }}
        columns={[
          { title: t('admin.colTime'), dataIndex: 'ts', width: 160, render: (ts: number) => new Date(ts * 1000).toLocaleString(ti18n.language) },
          { title: t('admin.colUser'), dataIndex: 'username', width: 100 },
          { title: t('admin.auditColAction'), dataIndex: 'action', width: 130,
            render: (v: string) => <Tag color={v ? 'blue' : 'default'}>{actLabel(v)}</Tag> },
          { title: t('admin.colMethod'), dataIndex: 'method', width: 70 },
          { title: t('admin.colPath'), dataIndex: 'path', ellipsis: true },
          { title: t('admin.colIp'), dataIndex: 'ip', width: 120 },
          { title: t('admin.colCode'), dataIndex: 'status_code', width: 70,
            render: (v: number) => <Tag color={v >= 400 ? 'error' : 'success'}>{v}</Tag> },
        ]} />
    </div>
  )
}

/** 容器运行日志：平台容器 + 任务容器 docker logs 查看 */
function ContainerTab({ active }: { active: boolean }) {
  const { t } = useTranslation()
  const [svcs, setSvcs] = useState<any[]>([])
  const [svc, setSvc] = useState('')
  const [tail, setTail] = useState(200)
  const [auto, setAuto] = useState(true)
  const [kw, setKw] = useState('')
  const [level, setLevel] = useState('all')
  const [lines, setLines] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const boxRef = useRef<HTMLPreElement>(null)

  const loadSvcs = () => {
    api.get('/api/logs/services').then((r) => {
      setSvcs(r.data || [])
      setSvc((cur) => (cur && (r.data || []).find((s) => s.name === cur) ? cur : (r.data || [])[0]?.name || ''))
    }).catch(() => {})
  }
  useEffect(loadSvcs, [])

  const loadLogs = (s: string, tl: number) => {
    if (!s) return
    api.get('/api/logs', { params: { service: s, tail: tl } })
      .then((r) => { setLines(r.data.lines || []); setRunning(!!r.data.running) })
      .catch(() => setLines([]))
  }
  useEffect(() => { loadLogs(svc, tail) }, [svc, tail])
  useEffect(() => {
    if (!auto || !active) return
    const x = setInterval(() => loadLogs(svc, tail), 5000)
    return () => clearInterval(x)
  }, [auto, svc, tail, active])
  // 新日志到达时自动滚到底部
  useEffect(() => { boxRef.current?.scrollTo(0, boxRef.current.scrollHeight) }, [lines])

  const visible = lines.filter((l) => {
    if (kw && !l.toLowerCase().includes(kw.toLowerCase())) return false
    if (level === 'error') return /\b(error|exception|traceback|fatal|panic|crit)\b/i.test(l)
    if (level === 'warn') return /\b(warn(ing)?|error|exception|traceback|fatal)\b/i.test(l)
    return true
  })
  const current = svcs.find((s) => s.name === svc)

  return (
    <div>
      <Space style={{ marginBottom: 12 }} wrap>
        <Select value={svc || undefined} style={{ width: 260 }} placeholder={t('logs.service')}
          onChange={(v) => setSvc(v)}
          options={svcs.map((s) => ({ value: s.name, label: s.name }))} />
        {current && (
          <Tag color={current.running ? 'success' : 'default'}>
            {current.running ? t('logs.running') : t('logs.stopped')}
          </Tag>
        )}
        <Select value={tail} style={{ width: 110 }} onChange={setTail}
          options={[100, 200, 500, 2000].map((x) => ({ value: x, label: `${t('logs.tail')}: ${x}` }))} />
        <Input placeholder={t('logs.searchPh')} value={kw} onChange={(e) => setKw(e.target.value)}
          style={{ width: 180 }} allowClear />
        <Select value={level} style={{ width: 100 }} onChange={setLevel}
          options={[
            { value: 'all', label: t('logs.all') },
            { value: 'warn', label: t('logs.warn') },
            { value: 'error', label: t('logs.error') },
          ]} />
        <Space size={4}>
          <Switch checked={auto} onChange={setAuto} checkedChildren={t('logs.auto')} unCheckedChildren={t('logs.auto')} />
          <Button size="small" icon={<ReloadOutlined />} onClick={() => loadLogs(svc, tail)} />
        </Space>
        <Button size="small" icon={<CopyOutlined />} onClick={() => {
          navigator.clipboard?.writeText(visible.join('\n'))
          message.success(t('logs.copied'))
        }}>{t('logs.copy')}</Button>
        <Button size="small" icon={<DownloadOutlined />} onClick={() => {
          const blob = new Blob([visible.join('\n')], { type: 'text/plain' })
          const a = document.createElement('a')
          a.href = URL.createObjectURL(blob)
          a.download = `vervedeck-${svc}-${Date.now()}.log`
          a.click()
          URL.revokeObjectURL(a.href)
        }}>{t('logs.download')}</Button>
      </Space>
      <Card size="small" bodyStyle={{ padding: 0 }}>
        <pre ref={boxRef} style={{
          margin: 0, padding: '10px 14px', height: 560, overflow: 'auto',
          background: '#111418', color: '#d8e0ea', fontSize: 12, lineHeight: 1.55,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
          whiteSpace: 'pre-wrap', wordBreak: 'break-all',
        }}>
          {visible.length ? visible.join('\n') : t('logs.empty')}
        </pre>
      </Card>
    </div>
  )
}
