import React, { useEffect, useRef, useState } from 'react'
import { Card, Select, Switch, Input, Button, Space, Tag, message } from 'antd'
import { CopyOutlined, DownloadOutlined, ReloadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

/** 日志中心（超管左菜单）：平台容器 + 任务容器 docker logs 查看 */
export default function LogsPage() {
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
    if (!auto) return
    const x = setInterval(() => loadLogs(svc, tail), 5000)
    return () => clearInterval(x)
  }, [auto, svc, tail])
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
      <h3 style={{ marginTop: 0 }}>{t('logs.title')}</h3>
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
