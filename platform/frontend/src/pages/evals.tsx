import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, Drawer,
  Descriptions, Statistic, Row, Col, message, Radio,
} from 'antd'
import { PlusOutlined, DownloadOutlined } from '@ant-design/icons'
import { BarChart, Bar, XAxis, YAxis, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'

const ST: Record<string, string> = { pending: 'default', running: 'processing', succeeded: 'success', failed: 'error' }
const BENCHES = ['gsm8k', 'ceval', 'arc', 'cmmlu', 'human_eval', 'mbpp']
const scoreOf = (s: any): number => (s?.avg_score != null ? s.avg_score : (s?.judge_avg != null ? s.judge_avg / 5 : 0))

export default function EvalCenter() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [sel, setSel] = useState<number[]>([])
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [deps, setDeps] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [datasets, setDatasets] = useState<any[]>([])
  const [dsVersions, setDsVersions] = useState<any[]>([])
  const [detail, setDetail] = useState<any>(null)
  const [compare, setCompare] = useState<any[]>([])
  const [targetKind, setTargetKind] = useState<'dep' | 'asset'>('dep')
  const [src, setSrc] = useState<'custom' | 'benchmark'>('custom')

  const load = useCallback(() => {
    api.get('/api/evals', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 6000); return () => clearInterval(t) }, [load])
  const batchDel = useBatchDelete('/api/evals/batch-delete', () => { setSel([]); load() })

  const openWizard = async () => {
    form.resetFields()
    form.setFieldsValue({ method: 'rule', limit: 50 })
    setTargetKind('dep'); setSrc('custom')
    setDsVersions([])
    try {
      const [d, m, ds] = await Promise.all([
        api.get('/api/inference/deployments', { params: { project_id: projectId } }),
        api.get('/api/models', { params: { project_id: projectId } }),
        api.get('/api/datasets', { params: { project_id: projectId } }),
      ])
      setDeps((d.data || []).filter((x: any) => x.status === 'running'))
      setModels((m.data || []).filter((x: any) => x.status === 'ready'))
      setDatasets(ds.data || [])
    } catch { /* ignore */ }
    setOpen(true)
  }

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('evals.title')}</h3>
        <Space>
          <Select mode="multiple" placeholder={t('evals.comparePh')} style={{ width: 320 }}
            options={list.filter((e) => e.status === 'succeeded').map((e) => ({ value: e.id, label: `#${e.id} ${e.name}` }))}
            onChange={(ids) => setCompare(list.filter((e) => ids.includes(e.id)))} />
          {sel.length > 0 && <Button danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button type="primary" icon={<PlusOutlined />} onClick={openWizard}>{t('evals.newEval')}</Button>
        </Space>
      </Space>
      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }}
        rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('evals.colTarget'), dataIndex: 'target', width: 180, ellipsis: true },
          { title: t('evals.colBench'), render: (_: any, r: any) => r.benchmark ? <Tag color="gold">{r.benchmark}</Tag> : (r.dataset_version || t('evals.custom')), width: 120 },
          { title: t('evals.colMethod'), dataIndex: 'method', width: 90 },
          { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={ST[v]}>{v}</Tag> },
          {
            title: t('evals.colScore'), width: 130, render: (_: any, r: any) => r.status === 'succeeded'
              ? <Space size={4}><b>{(r.summary?.avg_score ?? r.summary?.judge_avg ?? 0).toFixed?.(3) ?? r.summary?.avg_score ?? '-'}</b>
                {r.summary?.pass_rate != null && <span style={{ color: '#888' }}>pass {Math.round(r.summary.pass_rate * 100)}%</span>}</Space>
              : '-',
          },
          { title: t('common.createTime'), dataIndex: 'created_at', width: 170 },
          {
            title: t('common.actions'), width: 140, render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" onClick={() => api.get(`/api/evals/${r.id}`).then((x) => setDetail(x.data))}>{t('common.detail')}</Button>
                {r.status !== 'pending' && r.status !== 'running' && (
                  <Button size="small" danger onClick={() => Modal.confirm({
                    title: t('common.deleteConfirm'), okText: t('common.delete'), cancelText: t('common.cancel'),
                    onOk: async () => { await api.delete(`/api/evals/${r.id}`); message.success(t('common.deleted')); load() },
                  })}>{t('common.delete')}</Button>
                )}
              </Space>
            ),
          },
        ]} />

      {compare.length > 0 && (
        <Row gutter={16} style={{ marginTop: 12 }}>
          <Col span={10}>
            <div style={{ background: '#fafafa', padding: 12, borderRadius: 8 }}>
              <b>{t('evals.compareTitle')}</b>
              <ResponsiveContainer width="100%" height={240}>
                <BarChart data={compare.map((e) => ({ name: e.name.slice(0, 14), avg: scoreOf(e.summary), pass: (e.summary?.pass_rate ?? 0) * 100 }))}>
                  <XAxis dataKey="name" fontSize={11} /><YAxis domain={[0, 1]} fontSize={11} />
                  <RTooltip /><Legend />
                  <Bar dataKey="avg" name={t('evals.avgScore')} fill="#1677ff" />
                  <Bar dataKey="pass" name={t('evals.passRate')} fill="#52c41a" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Col>
        </Row>
      )}

      {/* 新建评测（基本信息 / 评测对象 / 评测数据 / 评测器） */}
      <Modal title={t('evals.newEval')} open={open} onCancel={() => setOpen(false)} width={640}
        onOk={async () => {
          const v = await form.validateFields()
          const isBench = src === 'benchmark' && !!v.benchmark
          if (!isBench && !v.dataset_version_id) { message.error(t('evals.select')); return }
          await api.post('/api/evals', {
            project_id: projectId, name: v.name,
            target_deployment_id: targetKind === 'dep' ? v.target_deployment_id : undefined,
            target_asset_id: targetKind === 'asset' ? v.target_asset_id : undefined,
            dataset_version_id: isBench ? undefined : v.dataset_version_id,
            benchmark: isBench ? v.benchmark : '', method: isBench ? 'benchmark' : v.method,
            judge_deployment_id: v.judge_deployment_id || undefined, judge_model: '',
            limit: v.limit,
          })
          message.success(t('evals.submitted'))
          setOpen(false); load()
        }}>
        <Form form={form} layout="vertical">
          <EvalSection title={t('evals.secBasic')}>
            <Form.Item name="name" label={t('evals.evalName')} rules={[{ required: true }]}><Input placeholder={t('evals.evalNamePh')} /></Form.Item>
          </EvalSection>
          <EvalSection title={t('evals.secTarget')}>
            <Form.Item label={t('evals.targetKind')} style={{ marginBottom: 8 }}>
              <Radio.Group value={targetKind} onChange={(e) => setTargetKind(e.target.value)} optionType="button" buttonStyle="solid"
                options={[{ value: 'dep', label: t('evals.targetDep') }, { value: 'asset', label: t('evals.targetAsset2') }]} />
            </Form.Item>
            {targetKind === 'dep' ? (
              <Form.Item name="target_deployment_id" label={t('evals.targetDep')} rules={[{ required: true, message: t('evals.select') }]}>
                <Select style={{ width: '100%' }} options={deps.map((d: any) => ({ value: d.id, label: `${d.name} (${d.model})` }))} />
              </Form.Item>
            ) : (
              <Form.Item name="target_asset_id" label={t('evals.targetAsset2')} rules={[{ required: true, message: t('evals.select') }]}>
                <Select style={{ width: '100%' }} options={models.map((m: any) => ({ value: m.id, label: `${m.name} (${m.type})` }))} />
              </Form.Item>
            )}
          </EvalSection>
          <EvalSection title={t('evals.secData')}>
            <Form.Item label={t('evals.dataSource')} style={{ marginBottom: 8 }}>
              <Radio.Group value={src} onChange={(e) => setSrc(e.target.value)} optionType="button" buttonStyle="solid"
                options={[{ value: 'custom', label: t('evals.srcCustom') }, { value: 'benchmark', label: t('evals.srcBench') }]} />
            </Form.Item>
            {src === 'custom' ? (
              <>
                <Form.Item label={t('evals.customSet')}>
                  <Select style={{ width: '100%' }} placeholder={t('evals.selectData')}
                    options={[...datasets].sort((a: any, b: any) => ((a.kind === 'eval' ? 0 : 1) - (b.kind === 'eval' ? 0 : 1)))
                      .map((d: any) => ({ value: d.id, label: `${d.name}${d.kind === 'eval' ? ` (${t('data.kindEval')})` : d.kind === 'train' ? ` (${t('data.kindTrain')})` : ''}` }))}
                    onChange={async (did) => {
                      form.setFieldsValue({ dataset_version_id: undefined })
                      if (did) setDsVersions((await api.get(`/api/datasets/${did}/versions`)).data)
                    }} />
                </Form.Item>
                <Form.Item name="dataset_version_id" label={t('evals.setVersion')} rules={[{ required: true, message: t('evals.select') }]}>
                  <Select style={{ width: '100%' }} options={dsVersions.map((v: any) => ({ value: v.id, label: `v${v.version} (${v.sample_count})` }))} />
                </Form.Item>
              </>
            ) : (
              <Form.Item name="benchmark" label={t('evals.benchmark')} rules={[{ required: true, message: t('evals.select') }]}>
                <Select style={{ width: '100%' }} options={BENCHES.map((b) => ({ value: b, label: b }))} placeholder={t('evals.benchPh')} />
              </Form.Item>
            )}
          </EvalSection>
          <EvalSection title={t('evals.secJudge')}>
            {src !== 'benchmark' && (
              <>
                <Form.Item name="method" label={t('evals.method')}>
                  <Radio.Group options={[
                    { value: 'rule', label: t('evals.rule') },
                    { value: 'judge', label: t('evals.judge') },
                    { value: 'both', label: t('evals.both') },
                  ]} />
                </Form.Item>
                <Form.Item noStyle shouldUpdate={(a, b) => a.method !== b.method}>
                  {({ getFieldValue }) => getFieldValue('method') !== 'rule' ? (
                    <Form.Item name="judge_deployment_id" label={t('evals.judgeModel')} rules={[{ required: true, message: t('evals.judgeReq') }]}>
                      <Select style={{ width: 240 }} options={deps.map((d: any) => ({ value: d.id, label: d.name }))} />
                    </Form.Item>
                  ) : null}
                </Form.Item>
              </>
            )}
            <Form.Item name="limit" label={t('evals.limit')}><InputNumber min={1} max={5000} /></Form.Item>
          </EvalSection>
        </Form>
      </Modal>

      <Drawer title={detail ? t('evals.detailTitleWith', { id: detail.id, name: detail.name }) : t('evals.detailTitle')} width={720} open={!!detail} onClose={() => setDetail(null)}
        extra={detail && (
          <Button icon={<DownloadOutlined />}
            onClick={() => api.get(`/api/evals/${detail.id}/report`, { responseType: 'blob' }).then((r) => {
              const url = URL.createObjectURL(new Blob([r.data]))
              const a = document.createElement('a')
              a.href = url; a.download = `eval-${detail.id}-report.md`; a.click()
              URL.revokeObjectURL(url)
            }).catch(() => message.error(t('api.requestFailed')))
          }>{t('evals.downloadReport')}</Button>
        )}>
        {detail && (
          <div>
            <Row gutter={12}>
              <Col span={6}><Card_s><Statistic title={t('evals.statAvg')} value={scoreOf(detail.summary)} precision={3} /></Card_s></Col>
              <Col span={6}><Card_s><Statistic title={t('evals.statPass')} value={`${Math.round((detail.summary?.pass_rate ?? 0) * 100)}%`} /></Card_s></Col>
              <Col span={6}><Card_s><Statistic title={t('evals.statJudge')} value={(detail.summary?.judge_avg ?? '-') as any} /></Card_s></Col>
              <Col span={6}><Card_s><Statistic title={t('evals.statTotal')} value={detail.summary?.total ?? '-'} /></Card_s></Col>
            </Row>
            {detail.summary?.judge_dist && (
              <p style={{ color: '#888' }}>{t('evals.judgeDist', { dist: Object.entries(detail.summary.judge_dist).map(([k, v]) => `${k}x${v}`).join('  ') })}</p>
            )}
            {detail.error && <p style={{ color: '#cf1322', whiteSpace: 'pre-wrap' }}>{detail.error}</p>}
            <h4>{t('evals.perSample')}</h4>
            <Table size="small" rowKey="index" dataSource={detail.details || []} pagination={{ pageSize: 8 }}
              columns={[
                { title: '#', dataIndex: 'index', width: 45 },
                { title: t('evals.colQ'), dataIndex: 'prompt', ellipsis: true, width: 200 },
                { title: t('evals.colRef'), dataIndex: 'reference', ellipsis: true, width: 160 },
                { title: t('evals.colPred'), dataIndex: 'pred', ellipsis: true },
                { title: t('evals.colRuleScore'), width: 130, render: (_: any, r: any) => r.rule ? `F1 ${r.rule.f1.toFixed(2)} / R ${r.rule.rouge_l.toFixed(2)} / ${r.rule.contains}` : '-' },
                { title: t('evals.judgeCol'), width: 150, render: (_: any, r: any) => r.judge ? t('evals.judgeScore', { s: r.judge.score, reason: r.judge.reason }) : '-' },
              ]} />
          </div>
        )}
      </Drawer>
    </div>
  )
}

function Card_s({ children }: { children: React.ReactNode }) {
  return <div style={{ background: '#fafafa', padding: '10px 14px', borderRadius: 8 }}>{children}</div>
}

// 分区标题（蓝色竖条 + 加粗标题）
function EvalSection({ title, children }: { title: string, children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ width: 4, height: 16, background: '#1677ff', marginRight: 8, borderRadius: 2 }} />
        <span style={{ fontWeight: 600 }}>{title}</span>
      </div>
      {children}
    </div>
  )
}
