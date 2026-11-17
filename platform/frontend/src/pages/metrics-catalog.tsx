// 监控指标页（集群运维 > 监控指标）：平台实际采集的指标目录（Prometheus 实时查询）。
// 顶部统计（活跃 series / 指标类型 / 类别分布）+ 指标表（搜索 + 类别筛选 + 客户端分页）。
// 对齐行业采集面：GPU 关键字段（DCGM）+ 宿主（node-exporter）+ 推理引擎（vLLM/SGLang）。
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { Table, Button, Space, Tag, Input, Select, Card, Row, Col, Statistic } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const PAGE = 50
const CAT_COLOR: Record<string, string> = { GPU: 'magenta', host: 'blue', inference: 'cyan', system: 'geekblue' }

export default function MetricsCatalogPage() {
  const { t } = useTranslation()
  const [data, setData] = useState<any>(null)
  const [err, setErr] = useState('')
  const [q, setQ] = useState('')
  const [cat, setCat] = useState('')

  const fetchCatalog = () =>
    api.get('/api/cluster/metrics/catalog')
      .then((r) => { setData(r.data); setErr('') })
      .catch((e: any) => setErr(e?.response?.data?.error || e?.message || ''))

  useEffect(() => { fetchCatalog() }, [])
  const pollRef = useRef<any>({})
  pollRef.current = { fetchCatalog }
  useEffect(() => {
    const x = setInterval(() => pollRef.current.fetchCatalog(), 30000)
    return () => clearInterval(x)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const catLabel = (c: string) =>
    c === 'GPU' ? 'GPU'
      : c === 'host' ? t('cluster.catHost')
      : c === 'inference' ? t('cluster.catInference')
      : c === 'system' ? t('cluster.catSystem')
      : t('cluster.catOther')

  const metrics: any[] = data?.metrics || []
  const filtered = useMemo(() => metrics.filter((m) =>
    (!cat || m.category === cat) &&
    (!q || m.name.toLowerCase().includes(q.toLowerCase()))
  ), [metrics, q, cat])

  const byCat: Record<string, number> = data?.by_category || {}
  const catKeys = Object.keys(byCat).sort((a, b) => byCat[b] - byCat[a])

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('cluster.tabMetrics')}</h3>
      {err && <Card size="small" style={{ marginBottom: 12 }}><Tag color="error">{err}</Tag></Card>}
      {data?.ok && (
        <Row gutter={12} style={{ marginBottom: 12 }}>
          <Col span={6}><Card size="small"><Statistic title={t('cluster.mcTotalSeries')} value={data.total_series} /></Card></Col>
          <Col span={6}><Card size="small"><Statistic title={t('cluster.mcTotalNames')} value={data.total_names} /></Card></Col>
          <Col span={12}><Card size="small" title={t('cluster.mcCategory')}>
            <Space size={12} wrap>
              {catKeys.map((c) => <Tag key={c} color={CAT_COLOR[c]}>{catLabel(c)}: {byCat[c]}</Tag>)}
            </Space>
          </Card></Col>
        </Row>
      )}
      <Card size="small" title={t('cluster.mcTitle')}
        extra={
          <Space size={6}>
            <Input size="small" allowClear placeholder={t('cluster.mcSearchPh')} style={{ width: 200 }}
              value={q} onChange={(e) => setQ(e.target.value)} />
            <Select size="small" allowClear placeholder={t('cluster.mcCategory')} style={{ width: 130 }}
              value={cat || undefined} onChange={(v) => setCat(v || '')}
              options={catKeys.map((c) => ({ value: c, label: catLabel(c) }))} />
            <Button size="small" icon={<ReloadOutlined />} onClick={fetchCatalog}>{t('common.refresh')}</Button>
          </Space>
        }>
        <Table size="small" rowKey="name" dataSource={filtered}
          pagination={{ pageSize: PAGE, showSizeChanger: false, showTotal: (n: number) => `${n}` }}
          locale={{ emptyText: '-' }}
          columns={[
            { title: t('cluster.mcName'), dataIndex: 'name', ellipsis: true,
              render: (v: string) => <code style={{ fontSize: 12 }}>{v}</code> },
            { title: t('cluster.mcCategory'), dataIndex: 'category', width: 110,
              render: (v: string) => <Tag color={CAT_COLOR[v]}>{catLabel(v)}</Tag> },
            { title: t('cluster.mcSource'), dataIndex: 'source', width: 180 },
            { title: t('cluster.mcSeries'), dataIndex: 'series', width: 110, align: 'right' as const,
              sorter: (a: any, b: any) => a.series - b.series },
          ]} />
      </Card>
    </div>
  )
}
