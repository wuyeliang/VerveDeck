import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, Steps,
  Drawer, Descriptions, message, Tooltip, Collapse, Checkbox,
} from 'antd'
import { PlusOutlined, StopOutlined, CaretRightOutlined, MergeCellsOutlined } from '@ant-design/icons'
import { LineChart, Line, XAxis, YAxis, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const ST: Record<string, string> = { queued: 'default', running: 'processing', succeeded: 'success', failed: 'error', cancelled: 'warning' }

// 按参数 spec 渲染对应控件（bool→Checkbox，choices→Select，数值→InputNumber，str→Input/TextArea）
const ParamControl = ({ p }: { p: any }) => {
  if (p.type === 'bool') return <Checkbox />
  if (p.choices) {
    return <Select allowClear={p.default === null} style={{ width: 200 }}
      options={p.choices.map((c: any) => ({ value: c, label: String(c) }))} />
  }
  if (p.type === 'int') return <InputNumber style={{ width: 150 }} min={p.min} max={p.max} step={p.step ?? 1} />
  if (p.type === 'float') return <InputNumber style={{ width: 190 }} min={p.min} max={p.max} step={p.step ?? 0.1} />
  if (p.key === 'extra_args') return <Input.TextArea rows={2} placeholder={'{"key": value}'} />
  return <Input style={{ width: 220 }} />
}

export default function TrainCenter() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState(0)
  const [form] = Form.useForm()
  const [models, setModels] = useState<any[]>([])
  const [datasets, setDatasets] = useState<any[]>([])
  const [detail, setDetail] = useState<any>(null)
  const [metrics, setMetrics] = useState<any>({})
  const [logs, setLogs] = useState('')
  const [stage, setStage] = useState('sft')
  const [dsVersions, setDsVersions] = useState<any[]>([])
  const [paramSpec, setParamSpec] = useState<any[]>([])
  const finetuningType = Form.useWatch('finetuning_type', form) || 'lora'

  const STAGE_LABEL: Record<string, string> = {
    cpt: t('train.stageCpt'), sft: t('train.stageSft'), dpo: t('train.stageDpo'), export: t('train.stageExport'),
  }

  useEffect(() => { api.get('/api/training/params').then((r) => setParamSpec(r.data || [])).catch(() => {}) }, [])

  const pickDataset = async (did: number) => {
    form.setFieldsValue({ dataset_version_ids: [] })
    setDsVersions([])
    if (did) {
      const r = await api.get(`/api/datasets/${did}/versions`)
      setDsVersions(r.data)
    }
  }

  const load = useCallback(() => {
    api.get('/api/training/jobs', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])

  // 参数分组显隐：LoRA/Freeze 跟随微调方式，DPO/导出跟随阶段
  const groupVisible = (g: any) => {
    if (g.key === 'lora') return finetuningType === 'lora' && stage !== 'export'
    if (g.key === 'freeze') return finetuningType === 'freeze'
    if (g.key === 'dpo') return stage === 'dpo'
    if (g.key === 'export') return stage === 'export'
    return true
  }

  const specDefaults = () => {
    const vals: any = {}
    for (const g of paramSpec) for (const p of g.params) if (p.default !== undefined && p.default !== null) vals[p.key] = p.default
    return vals
  }

  const openWizard = () => {
    setStep(0)
    form.resetFields()
    form.setFieldsValue({ stage: 'sft', finetuning_type: 'lora', priority: 5, gpu_count: 1, deepspeed: 'none', ...specDefaults() })
    api.get('/api/models', { params: { project_id: projectId } }).then((r) => setModels(r.data.filter((m: any) => m.status === 'ready'))).catch(() => {})
    api.get('/api/datasets', { params: { project_id: projectId } }).then((r) => setDatasets(r.data)).catch(() => {})
    setOpen(true)
  }

  // 进入超参数步时：先铺 spec 默认值，再叠阶段模板（模板优先）
  const applyTemplate = async (s: string) => {
    form.setFieldsValue(specDefaults())
    const r = await api.get('/api/training/templates')
    form.setFieldsValue(r.data[s] || {})
  }

  // 超参数步：收集所有 spec 分组里当前值（剔除 undefined/空串）
  const collectHyperparams = () => {
    const v = form.getFieldsValue(true)
    const out: any = {}
    for (const g of paramSpec) for (const p of g.params) {
      const val = v[p.key]
      if (val === undefined || val === null || val === '') continue
      out[p.key] = val
    }
    return out
  }

  const submit = async () => {
    const v = form.getFieldsValue()
    await api.post('/api/training/jobs', {
      project_id: projectId, name: v.name, stage: v.stage, finetuning_type: v.finetuning_type,
      base_model_id: v.base_model_id, adapter_asset_id: v.adapter_asset_id || null,
      dataset_version_ids: v.dataset_version_ids || [],
      hyperparams: collectHyperparams(),
      deepspeed: v.deepspeed, gpu_count: v.gpu_count, priority: v.priority,
    })
    message.success(t('train.enqueued'))
    setOpen(false)
    load()
  }

  const openDetail = (id: number) => {
    setDetail(null)
    api.get(`/api/training/jobs/${id}`).then((r) => setDetail(r.data)).catch(() => {})
  }
  useEffect(() => {
    if (!detail) return
    const t = setInterval(() => {
      api.get(`/api/training/jobs/${detail.id}/metrics`).then((r) => setMetrics(r.data)).catch(() => {})
      api.get(`/api/training/jobs/${detail.id}/logs`, { params: { tail: 120 } }).then((r) => setLogs(r.data.logs)).catch(() => {})
      api.get(`/api/training/jobs/${detail.id}`).then((r) => setDetail(r.data)).catch(() => {})
    }, 3000)
    return () => clearInterval(t)
  }, [detail?.id])

  const lossData = (metrics.loss || []).map((p: any, i: number) => ({ step: p[0], loss: p[1], epoch: (metrics.epoch?.[i] || [])[0] !== undefined ? metrics.epoch[i][1] : undefined }))
  const lrData = metrics.learning_rate || []

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('train.title')}</h3>
        <Button type="primary" icon={<PlusOutlined />} onClick={openWizard}>{t('train.newJob')}</Button>
      </Space>
      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('train.colStage'), dataIndex: 'stage', width: 90, render: (v: string) => <Tag>{v.toUpperCase()}</Tag> },
          { title: t('train.colMethod'), dataIndex: 'finetuning_type', width: 100 },
          { title: t('train.colBase'), dataIndex: 'base_model', width: 170, ellipsis: true },
          { title: t('train.dGpu'), dataIndex: 'gpu_count', width: 60 },
          { title: t('train.colPriority'), dataIndex: 'priority', width: 70 },
          { title: t('common.status'), dataIndex: 'status', width: 110, render: (v: string) => <Tag color={ST[v]}>{v}</Tag> },
          { title: t('common.createTime'), dataIndex: 'created_at', width: 170 },
          { title: t('common.actions'), width: 160, render: (_: any, r: any) => (
            <Space size={4}>
              <Button size="small" onClick={() => openDetail(r.id)}>{t('common.detail')}</Button>
              {r.status === 'queued' && <Tooltip title={t('train.cancelTip')}><Button size="small" danger icon={<StopOutlined />} onClick={() => api.post(`/api/training/jobs/${r.id}/cancel`).then(() => load())} /></Tooltip>}
              {r.status === 'failed' && <Tooltip title={t('train.resumeTip')}><Button size="small" icon={<CaretRightOutlined />} onClick={() => api.post(`/api/training/jobs/${r.id}/resume`, {}).then(() => { message.success(t('train.resumeEnqueued')); load() })} /></Tooltip>}
            </Space>
          )},
        ]} />

      <Modal title={t('train.newJob')} open={open} onCancel={() => setOpen(false)} width={720}
        footer={[
          <Button key="b" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>{t('train.prev')}</Button>,
          step < 2
            ? <Button key="n" type="primary" onClick={async () => { try { await form.validateFields(); setStep(step + 1); if (step === 0) applyTemplate(form.getFieldValue('stage')) } catch { } }}>{t('train.next')}</Button>
            : <Button key="s" type="primary" onClick={submit}>{t('train.submit')}</Button>,
        ]}>
        <Steps size="small" current={step} items={[{ title: t('train.stepBase') }, { title: t('train.stepHyper') }, { title: t('train.stepCompute') }]} style={{ margin: '8px 0 20px' }} />
        <Form form={form} layout="vertical">
          <div style={{ display: step === 0 ? 'block' : 'none' }}>
            <Space wrap>
              <Form.Item name="name" label={t('train.jobName')} rules={[{ required: true }]}><Input style={{ width: 220 }} placeholder={t('train.jobNamePh')} /></Form.Item>
              <Form.Item name="stage" label={t('train.stage')} rules={[{ required: true }]}>
                <Select style={{ width: 200 }} options={Object.entries(STAGE_LABEL).map(([v, l]) => ({ value: v, label: l }))} onChange={(v) => setStage(v)} />
              </Form.Item>
            </Space>
            <Space wrap>
              <Form.Item name="finetuning_type" label={t('train.method')}>
                <Select style={{ width: 140 }} options={[{ value: 'lora', label: t('train.ftLora') }, { value: 'full', label: t('train.ftFull') }, { value: 'freeze', label: t('train.ftFreeze') }]} />
              </Form.Item>
              <Form.Item name="base_model_id" label={t('train.baseModel')} rules={[{ required: true }]}>
                <Select style={{ width: 260 }} options={models.map((m: any) => ({ value: m.id, label: `${m.name} (${m.type})` }))} />
              </Form.Item>
              <Form.Item name="priority" label={t('train.priority')}><InputNumber min={1} max={9} /></Form.Item>
            </Space>
            {stage !== 'export' && (
              <>
                <Form.Item label={t('train.trainData')} required>
                  <Select style={{ width: 320 }} placeholder={t('train.selectData')}
                    options={datasets.map((d: any) => ({ value: d.id, label: d.name }))}
                    onChange={pickDataset} />
                </Form.Item>
                <Form.Item name="dataset_version_ids" label={t('train.dsVersions')} rules={[{ required: true, message: t('train.selectVersion') }]}>
                  <Select mode="multiple" style={{ width: '100%' }}
                    options={dsVersions.map((v: any) => ({ value: v.id, label: `v${v.version} · ${v.sample_count} · ${v.status} · ${v.note || ''}` }))} />
                </Form.Item>
              </>
            )}
            {stage === 'export' && (
              <>
                <p style={{ color: '#888' }}>{t('train.exportIntro')}</p>
                <Form.Item name="adapter_asset_id" label={t('train.loraArtifact')} rules={[{ required: true, message: t('train.selectLoraArtifact') }]}>
                  <Select style={{ width: 320 }} placeholder={t('train.loraPlaceholder')}
                    options={models.filter((m: any) => m.type === 'lora').map((m: any) => ({ value: m.id, label: m.name }))} />
                </Form.Item>
              </>
            )}
          </div>
          <div style={{ display: step === 1 ? 'block' : 'none' }}>
            {/* 超参数：由 GET /api/training/params 的 spec 动态渲染，覆盖 LLaMA-Factory WebUI 全量参数 */}
            <Collapse size="small"
              defaultActiveKey={paramSpec.filter((g) => groupVisible(g) && g.open).map((g) => g.key)}
              items={paramSpec.filter((g) => groupVisible(g)).map((g) => ({
                key: g.key,
                label: t('pg.' + g.key, g.label) as string,
                children: (
                  <Space wrap>
                    {g.params.map((p: any) => (
                      <Form.Item key={p.key} name={p.key} label={t('pp.' + p.key, p.label) as string}
                        style={{ marginBottom: 12, maxWidth: p.key === 'extra_args' ? 520 : undefined }}>
                        <ParamControl p={p} />
                      </Form.Item>
                    ))}
                  </Space>
                ),
              }))} />
            {paramSpec.length === 0 && <p style={{ color: '#888' }}>{t('train.paramLoading')}</p>}
          </div>
          <div style={{ display: step === 2 ? 'block' : 'none' }}>
            <Space wrap>
              <Form.Item name="deepspeed" label={t('train.deepspeed')}>
                <Select style={{ width: 140 }} options={[{ value: 'none', label: t('train.noDs') }, { value: 'zero2', label: t('train.dsZero2') }, { value: 'zero3', label: t('train.dsZero3') }]} />
              </Form.Item>
              <Form.Item name="gpu_count" label={t('train.gpuCount')}><InputNumber min={1} max={4} /></Form.Item>
            </Space>
            <Descriptions size="small" column={2} style={{ marginTop: 12 }}
              items={[
                { key: 1, label: t('train.summaryName'), children: form.getFieldValue('name') },
                { key: 2, label: t('train.dStage'), children: STAGE_LABEL[stage] },
                { key: 3, label: t('train.colBase'), children: models.find((m) => m.id === form.getFieldValue('base_model_id'))?.name },
                { key: 4, label: t('train.summarySchedule'), children: t('train.scheduleDesc') },
              ]} />
          </div>
        </Form>
      </Modal>

      <Drawer title={detail ? t('train.detailTitleWith', { id: detail.id, name: detail.name }) : t('train.detailTitle')} width={760}
        open={!!detail} onClose={() => setDetail(null)}
        extra={detail?.status === 'running' && (
          <Button danger size="small" icon={<StopOutlined />} onClick={async () => { await api.post(`/api/training/jobs/${detail.id}/cancel`); openDetail(detail.id) }}>{t('train.cancelTip')}</Button>
        )}>
        {detail && (
          <div>
            <Descriptions size="small" column={3} bordered style={{ marginBottom: 16 }}
              items={[
                { key: 1, label: t('common.status'), children: <Tag color={ST[detail.status]}>{detail.status}</Tag> },
                { key: 2, label: t('train.dStage'), children: detail.stage },
                { key: 3, label: t('train.dMethod'), children: detail.finetuning_type },
                { key: 4, label: t('train.colBase'), children: detail.base_model },
                { key: 5, label: t('train.dGpu'), children: detail.gpu_count },
                { key: 6, label: t('train.deepspeed'), children: detail.deepspeed },
              ]} />
            {detail.error && <p style={{ color: '#cf1322', whiteSpace: 'pre-wrap' }}>{detail.error}</p>}
            <h4>{t('train.lossTitle')}</h4>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={lossData}>
                <XAxis dataKey="step" fontSize={11} />
                <YAxis domain={['auto', 'auto']} fontSize={11} />
                <RTooltip />
                <Legend />
                <Line type="monotone" dataKey="loss" name="loss" stroke="#1677ff" dot={false} />
              </LineChart>
            </ResponsiveContainer>
            {lrData.length > 0 && (
              <>
                <h4>{t('train.lrTitle')}</h4>
                <ResponsiveContainer width="100%" height={140}>
                  <LineChart data={lrData.map((p: any) => ({ step: p[0], lr: p[1] }))}>
                    <XAxis dataKey="step" fontSize={11} /><YAxis fontSize={10} />
                    <RTooltip /><Line type="monotone" dataKey="lr" stroke="#fa8c16" dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </>
            )}
            <h4>{t('train.ckptTitle')}</h4>
            <Space wrap>{(detail.checkpoints || []).map((c: any) => <Tag key={c.step}>checkpoint-{c.step}</Tag>)}</Space>
            {detail.status === 'succeeded' && detail.finetuning_type === 'lora' && (
              <Button style={{ marginTop: 8 }} icon={<MergeCellsOutlined />}
                onClick={async () => {
                  const loraAsset = await api.get('/api/models', { params: { project_id: projectId } }).then((r) => r.data.find((m: any) => m.job_id === detail.id && m.type === 'lora'))
                  if (!loraAsset) { message.warning(t('train.noLoraAsset')); return }
                  await api.post('/api/training/jobs', {
                    project_id: projectId, name: detail.name + '-merged', stage: 'export',
                    finetuning_type: 'lora', base_model_id: detail.base_model_id,
                    adapter_asset_id: loraAsset.id, dataset_version_ids: [],
                    hyperparams: { template: 'qwen', export_size: 2, export_device: 'cpu' },
                    deepspeed: 'none', gpu_count: 1, priority: detail.priority,
                  })
                  message.success(t('train.mergeEnqueued')); openDetail(detail.id)
                }}>{t('train.mergeExport')}</Button>
            )}
            <h4>{t('train.logsTitle')}</h4>
            <pre style={{ background: '#f6f8fa', padding: 12, borderRadius: 6, maxHeight: 260, overflow: 'auto', fontSize: 12 }}>{logs || t('train.noLogs')}</pre>
          </div>
        )}
      </Drawer>
    </div>
  )
}
