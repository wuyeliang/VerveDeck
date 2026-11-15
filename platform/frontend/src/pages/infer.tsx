import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, Drawer,
  Descriptions, message, Popconfirm, Card, Tabs, Row, Col, Statistic,
} from 'antd'
import {
  PlusOutlined, PoweroffOutlined, PlayCircleOutlined, CopyOutlined,
  CodeOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import { LineChart, Line, XAxis, YAxis, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const ST: Record<string, string> = { creating: 'processing', running: 'success', stopped: 'default', failed: 'error' }
// 引擎镜像由平台级 pin 决定（与 compose IMAGES_VLLM/IMAGES_SGLANG 一致），按引擎展示
const ENGINE_IMAGES: Record<string, string> = {
  vllm: 'vllm/vllm-openai:v0.33.0',
  sglang: 'lmsysorg/sglang:v0.5.23',
}

export default function Inference() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [models, setModels] = useState<any[]>([])
  const [loraModels, setLoraModels] = useState<any[]>([])
  const [detail, setDetail] = useState<any>(null)
  const [logs, setLogs] = useState('')
  const [chat, setChat] = useState<any[]>([])
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [addAda, setAddAda] = useState<any>(null)
  const [mon, setMon] = useState<any>(null)
  const [nodeName, setNodeName] = useState('')
  const tp = Form.useWatch('tensor_parallel', form) || 1
  const rep = Form.useWatch('replicas', form) || 1
  const eng = Form.useWatch('engine', form) || 'vllm'

  const load = useCallback(() => {
    api.get('/api/inference/deployments', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])
  // 节点名（当前单节点集群，用于列表「节点」列与部署表单 placement 展示）
  useEffect(() => {
    api.get('/api/cluster/nodes').then((r) => setNodeName(r.data?.[0]?.name || '')).catch(() => {})
  }, [])

  const copy = (text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => message.success(t('infer.copied')),
      () => message.error(t('infer.copy')),
    )
  }

  const openDetail = (id: number) => {
    api.get(`/api/inference/deployments/${id}`).then((r) => setDetail(r.data)).catch(() => {})
    setChat([])
  }
  useEffect(() => {
    if (!detail) return
    const t = setInterval(() => {
      api.get(`/api/inference/deployments/${detail.id}/logs`, { params: { tail: 80 } }).then((r) => setLogs(r.data.logs)).catch(() => {})
      api.get(`/api/inference/deployments/${detail.id}`).then((r) => setDetail(r.data)).catch(() => {})
    }, 4000)
    return () => clearInterval(t)
  }, [detail?.id])

  // 引擎监控（KV 缓存 / 请求数 / 延迟）：15s 轮询
  useEffect(() => {
    if (!detail) { setMon(null); return }
    let stop = false
    const loadMon = () => api.get(`/api/inference/deployments/${detail.id}/metrics?minutes=60`)
      .then((r) => { if (!stop) setMon(r.data) }).catch(() => {})
    loadMon()
    const x = setInterval(loadMon, 15000)
    return () => { stop = true; clearInterval(x) }
  }, [detail?.id])

  const sendChat = async () => {
    if (!chatInput || !detail) return
    const msgs = [...chat, { role: 'user', content: chatInput }]
    setChat(msgs)
    setChatInput('')
    setChatLoading(true)
    try {
      const r = await api.post(`/v1/${detail.name}/chat/completions`, {
        model: detail.name, messages: msgs, stream: false,
      })
      setChat([...msgs, { role: 'assistant', content: r.data.choices?.[0]?.message?.content || '' }])
    } catch (e: any) {
      setChat([...msgs, { role: 'assistant', content: '⚠️ ' + (e.response?.data?.error?.message || e.message) }])
    } finally {
      setChatLoading(false)
    }
  }

  const openWizard = async () => {
    form.resetFields()
    form.setFieldsValue({ engine: 'vllm', quantization: 'none', tensor_parallel: 1, max_model_len: 8192, gpu_mem_util: 0.85, replicas: 1 })
    try {
      const m = await api.get('/api/models', { params: { project_id: projectId } })
      const all = m.data || []
      setModels(all.filter((x: any) => x.status === 'ready' && (x.type === 'base' || x.type === 'merged')))
      setLoraModels(all.filter((x: any) => x.type === 'lora'))
    } catch { /* ignore */ }
    setOpen(true)
  }

  // 服务调用代码示例
  const base = detail ? `${location.origin}/v1/${detail.name}` : ''
  const codeCurl = `curl ${base}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "x-api-key: <PROJECT_API_KEY>" \\
  -d '{"model": "${detail?.name || ''}", "messages": [{"role": "user", "content": "你好"}]}'`
  const codePy = `from openai import OpenAI

client = OpenAI(
    base_url="${base}",
    api_key="<PROJECT_API_KEY>",   # 或传 JWT 并改用 authorization
)
resp = client.chat.completions.create(
    model="${detail?.name || ''}",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)`
  const codeNode = `const OpenAI = require("openai");

const client = new OpenAI({
  baseURL: "${base}",
  apiKey: "<PROJECT_API_KEY>",
});
const resp = await client.chat.completions.create({
  model: "${detail?.name || ''}",
  messages: [{ role: "user", content: "你好" }],
});
console.log(resp.choices[0].message.content)`

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('infer.title')}</h3>
        <Button type="primary" icon={<PlusOutlined />} onClick={openWizard}>{t('infer.newDeploy')}</Button>
      </Space>
      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }} scroll={{ x: 1280 }}
        columns={[
          { title: t('common.name'), dataIndex: 'name', width: 165, ellipsis: true, fixed: 'left' },
          { title: t('infer.colModel'), dataIndex: 'model', width: 150, ellipsis: true },
          { title: t('infer.colEngine'), dataIndex: 'engine', width: 78, render: (v: string) => <Tag color={v === 'vllm' ? 'blue' : 'cyan'}>{v}</Tag> },
          { title: t('infer.colImage'), dataIndex: 'engine', key: 'image', width: 180, ellipsis: true,
            render: (v: string) => <code style={{ fontSize: 11 }}>{ENGINE_IMAGES[v] || v}</code> },
          { title: t('infer.colNode'), width: 105, render: () => (nodeName ? <Tag>{nodeName}</Tag> : <span style={{ color: '#bbb' }}>-</span>) },
          { title: t('infer.colQuant'), dataIndex: 'quantization', width: 78,
            render: (v: string) => v === 'none' ? <span style={{ color: '#bbb' }}>-</span> : <Tag color="purple">{v}</Tag> },
          { title: t('infer.tp'), dataIndex: 'tensor_parallel', width: 52 },
          { title: t('infer.colReplicas'), dataIndex: 'replicas', width: 62 },
          {
            title: t('infer.endpoint'), width: 190, render: (_: any, r: any) => r.status === 'running'
              ? <Space size={4}><code style={{ fontSize: 12 }}>/v1/{r.name}</code>
                <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copy(`${location.origin}/v1/${r.name}`)} /></Space>
              : <span style={{ color: '#bbb' }}>-</span>,
          },
          { title: t('common.status'), dataIndex: 'status', width: 96, render: (v: string) => <Tag color={ST[v]}>{v}</Tag> },
          {
            title: t('common.actions'), width: 220, render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" onClick={() => openDetail(r.id)}>{t('common.detail')}</Button>
                {r.status !== 'running' && r.status !== 'creating' &&
                  <Button size="small" icon={<PlayCircleOutlined />} onClick={() => api.post(`/api/inference/deployments/${r.id}/start`).then(() => message.info(t('infer.startInfo')))}>{t('infer.start')}</Button>}
                {r.status === 'running' &&
                  <Button size="small" danger icon={<PoweroffOutlined />} onClick={() => api.post(`/api/inference/deployments/${r.id}/stop`).then(() => message.success(t('infer.stopped')))}>{t('infer.stop')}</Button>}
                <Popconfirm title={t('infer.deleteConfirm')} onConfirm={() => api.delete(`/api/inference/deployments/${r.id}`).then(() => load())}>
                  <Button size="small" danger>{t('common.delete')}</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]} />

      {/* 新建部署（百炼风格：基本信息 / 模型选择 / 算力资源 / 运行参数） */}
      <Modal title={t('infer.modalTitle')} open={open} onCancel={() => setOpen(false)} width={680}
        onOk={async () => {
          const v = await form.validateFields()
          const adapters: any[] = []
          ;(v.lora_assets || []).forEach((aid: number) => {
            const a = loraModels.find((x: any) => x.id === aid)
            if (a) adapters.push({ name: a.name.replace(/[^a-zA-Z0-9_-]/g, '_'), path: a.path })
          })
          await api.post('/api/inference/deployments', {
            project_id: projectId, name: v.name, model_asset_id: v.model_asset_id,
            engine: v.engine, quantization: v.quantization, tensor_parallel: v.tensor_parallel,
            max_model_len: v.max_model_len, gpu_mem_util: v.gpu_mem_util,
            enable_lora: adapters.length > 0, lora_adapters: adapters, replicas: v.replicas,
            description: v.description || '',
          })
          message.success(t('infer.created'))
          setOpen(false); load()
        }}>
        <Form form={form} layout="vertical">
          <InferSection title={t('infer.secBasic')}>
            <Form.Item name="name" label={t('infer.deployName')} rules={[{ required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: t('infer.nameRule') }]}>
              <Input placeholder={t('infer.namePh')} maxLength={60} showCount />
            </Form.Item>
            <Form.Item name="description" label={t('infer.desc')}>
              <Input.TextArea placeholder={t('infer.descPh')} maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
            </Form.Item>
          </InferSection>
          <InferSection title={t('infer.secModel')}>
            <Form.Item name="model_asset_id" label={t('infer.modelAsset')} rules={[{ required: true }]}>
              <Select options={models.map((m: any) => ({ value: m.id, label: `${m.name} (${m.type})` }))} />
            </Form.Item>
          </InferSection>
          <InferSection title={t('infer.secResource')}>
            <Space wrap align="start">
              <Form.Item name="engine" label={t('infer.engine')}>
                <Select style={{ width: 130 }} options={[{ value: 'vllm', label: 'vLLM' }, { value: 'sglang', label: 'SGLang' }]} />
              </Form.Item>
              <Form.Item label={t('infer.dImage')}>
                <code style={{ fontSize: 12, background: '#f5f5f5', padding: '4px 8px', borderRadius: 4 }}>
                  {ENGINE_IMAGES[eng] || '—'}
                </code>
              </Form.Item>
            </Space>
            <Space wrap align="start">
              <Form.Item name="tensor_parallel" label={t('infer.tp')}><InputNumber min={1} max={8} /></Form.Item>
              <Form.Item name="replicas" label={t('infer.replicas')}><InputNumber min={1} max={8} /></Form.Item>
              <Form.Item label={t('infer.formNode')} tooltip={t('infer.formNodeHint')}>
                <Input disabled value={nodeName || '—'} style={{ width: 170 }} />
              </Form.Item>
            </Space>
            <div style={{ color: '#888', fontSize: 12, marginTop: -4 }}>
              <ThunderboltOutlined /> {t('infer.gpuNeed', { n: tp * rep, tp, r: rep })}
            </div>
          </InferSection>
          <InferSection title={t('infer.secParams')}>
            <Space wrap>
              <Form.Item name="quantization" label={t('infer.quant')}>
                <Select style={{ width: 120 }} options={['none', 'awq', 'gptq', 'int8'].map((q) => ({ value: q, label: q }))} />
              </Form.Item>
              <Form.Item name="max_model_len" label={t('infer.maxLen')}><InputNumber min={1024} step={1024} /></Form.Item>
              <Form.Item name="gpu_mem_util" label={t('infer.memUtil')}><InputNumber step={0.05} min={0.3} max={0.95} /></Form.Item>
            </Space>
            <Form.Item name="lora_assets" label={t('infer.extraLora')} style={{ marginTop: 4 }}>
              <Select mode="multiple" options={loraModels.map((m: any) => ({ value: m.id, label: m.name }))} placeholder={t('common.optional')} />
            </Form.Item>
          </InferSection>
        </Form>
      </Modal>

      <Drawer title={detail ? t('infer.detailTitleWith', { name: detail.name }) : t('infer.detailTitle')} width={760} open={!!detail} onClose={() => setDetail(null)}>
        {detail && (
          <Tabs items={[
            {
              key: 'invoke', label: <span><CodeOutlined /> {t('infer.tabInvoke')}</span>, children: (
                <div>
                  <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}
                    items={[
                      { key: 1, label: t('common.status'), children: <Tag color={ST[detail.status]}>{detail.status}</Tag> },
                      { key: 2, label: t('common.desc'), children: detail.description || '-' },
                      { key: 3, label: t('infer.dEngine'), children: `${detail.engine} / ${detail.quantization} / TP ${detail.tensor_parallel} × ${detail.replicas}` },
                      { key: 'img', label: t('infer.dImage'), children: <code style={{ fontSize: 12 }}>{ENGINE_IMAGES[detail.engine] || detail.engine}</code> },
                      { key: 'node', label: t('infer.colNode'), children: nodeName || '-' },
                      { key: 4, label: t('infer.colModel'), children: detail.model },
                      { key: 5, label: t('infer.dMaxLen'), children: `${detail.max_model_len} / ${detail.gpu_mem_util}` },
                      {
                        key: 6, label: t('infer.baseUrl'),
                        children: <Space><code>{base}</code>
                          <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copy(base)} /></Space>,
                      },
                    ]} />
                  <p style={{ color: '#888', fontSize: 12, marginTop: -8 }}>{t('infer.invokeHint')}</p>
                  {detail.error && <p style={{ color: '#cf1322', whiteSpace: 'pre-wrap' }}>{detail.error}</p>}
                  <Card size="small" title={t('infer.example')} style={{ marginBottom: 12 }}
                    extra={<Button size="small" type="text" icon={<CopyOutlined />}
                      onClick={() => copy(codeCurl)}>{t('infer.copy')}</Button>}>
                    <pre style={{ fontSize: 12, margin: 0, whiteSpace: 'pre-wrap' }}>{codeCurl}</pre>
                  </Card>
                  <Tabs size="small" items={[
                    { key: 'py', label: 'Python', children: <CodeBlock code={codePy} onCopy={copy} /> },
                    { key: 'node', label: 'Node.js', children: <CodeBlock code={codeNode} onCopy={copy} /> },
                  ]} />
                </div>
              ),
            },
            {
              key: 'play', label: <span><ThunderboltOutlined /> {t('infer.tabPlay')}</span>, children: (
                <div style={{ display: 'flex', flexDirection: 'column', height: 480 }}>
                  <div style={{ flex: 1, overflow: 'auto', background: '#fafafa', borderRadius: 8, padding: 12 }}>
                    {chat.length === 0 && <span style={{ color: '#999' }}>{t('infer.playHint')}</span>}
                    {chat.map((m, i) => (
                      <div key={i} style={{ marginBottom: 10 }}>
                        <Tag color={m.role === 'user' ? 'blue' : 'green'}>{m.role}</Tag>
                        <div style={{ whiteSpace: 'pre-wrap', marginTop: 4 }}>{m.content}</div>
                      </div>
                    ))}
                    {chatLoading && <span style={{ color: '#888' }}>{t('infer.thinking')}</span>}
                  </div>
                  <Space.Compact style={{ marginTop: 10 }}>
                    <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} value={chatInput}
                      onChange={(e) => setChatInput(e.target.value)}
                      onPressEnter={(e) => { if (!e.shiftKey) { e.preventDefault(); sendChat() } }} placeholder={t('infer.chatPh')} />
                    <Button type="primary" loading={chatLoading} disabled={detail.status !== 'running'} onClick={sendChat}>{t('infer.send')}</Button>
                  </Space.Compact>
                </div>
              ),
            },
            {
              key: 'monitor', label: t('infer.tabMonitor'), children: (
                <div>
                  <MonitorPanel mon={mon} />
                </div>
              ),
            },
            {
              key: 'lora', label: t('infer.tabLora', { n: detail.lora_adapters?.length || 0 }), children: (
                <div>
                  <Space style={{ marginBottom: 12 }}>
                    <Select style={{ width: 280 }} placeholder={t('infer.selectLora')}
                      options={loraModels.map((m: any) => ({ value: m.id, label: m.name }))}
                      onChange={(v) => setAddAda(v)} />
                    <Button type="primary" disabled={!addAda || detail.status !== 'running'}
                      onClick={async () => {
                        const a = loraModels.find((x: any) => x.id === addAda)
                        const r = await api.post(`/api/inference/deployments/${detail.id}/adapters`,
                          { name: a.name.replace(/[^a-zA-Z0-9_-]/g, '_'), path: a.path })
                        if (r.data.ok) message.success(t('infer.loaded'))
                        else message.error(r.data.error || t('infer.loadFail'))
                        setAddAda(null); openDetail(detail.id)
                      }}>{t('infer.hotload')}</Button>
                  </Space>
                  <Table size="small" rowKey="name" dataSource={detail.lora_adapters || []} pagination={false}
                    columns={[
                      { title: t('common.name'), dataIndex: 'name' },
                      { title: t('models.path'), dataIndex: 'path', ellipsis: true },
                      { title: t('common.actions'), width: 90, render: (_: any, r: any) => (
                        <Button size="small" danger onClick={async () => { await api.delete(`/api/inference/deployments/${detail.id}/adapters/${r.name}`); openDetail(detail.id) }}>{t('infer.unload')}</Button>
                      )},
                    ]} />
                </div>
              ),
            },
            {
              key: 'logs', label: t('infer.tabLogs'), children: (
                <pre style={{ background: '#f6f8fa', padding: 12, borderRadius: 6, maxHeight: 480, overflow: 'auto', fontSize: 12 }}>{logs || t('infer.noLogs')}</pre>
              ),
            },
            {
              key: 'scale', label: t('infer.tabScale'), children: (
                <Space>
                  <span>{t('infer.curReplicas', { n: detail.replicas })}</span>
                  <Button onClick={() => api.post(`/api/inference/deployments/${detail.id}/scale`, { replicas: detail.replicas + 1 }).then(() => message.success(t('infer.scaledUp')))}>{t('infer.scaleUp')}</Button>
                  <Button disabled={detail.replicas <= 1} onClick={() => api.post(`/api/inference/deployments/${detail.id}/scale`, { replicas: detail.replicas - 1 }).then(() => message.success(t('infer.scaledDown')))}>{t('infer.scaleDown')}</Button>
                </Space>
              ),
            },
          ]} />
        )}
      </Drawer>
    </div>
  )
}

// 分区标题（百炼风格：蓝色竖条 + 加粗标题）
function InferSection({ title, children }: { title: string, children: React.ReactNode }) {
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

/** 引擎监控：KV 缓存 / 运行中·排队请求 / 平均延迟（约 30s 采集一次，保留 24h） */
function MonitorPanel({ mon }: { mon: any }) {
  const { t, i18n: ti18n } = useTranslation()
  const ser = mon?.series || []
  const last = ser[ser.length - 1]
  const data = ser.map((x: any) => ({
    t: new Date(x.ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }),
    kv: Math.round(x.kv * 10) / 10, running: x.running, waiting: x.waiting,
  }))
  if (!last) return <span style={{ color: '#999' }}>{t('infer.monEmpty')}</span>
  return (
    <div>
      <p style={{ color: '#888', fontSize: 12, marginTop: 0 }}>{t('infer.monHint')}</p>
      <Row gutter={16}>
        <Col span={6}><Statistic title={t('infer.kvCache')} value={last.kv} suffix="%" precision={1} valueStyle={{ color: last.kv > 90 ? '#cf1322' : undefined }} /></Col>
        <Col span={6}><Statistic title={t('infer.runReqs')} value={last.running} /></Col>
        <Col span={6}><Statistic title={t('infer.waitReqs')} value={last.waiting} valueStyle={{ color: last.waiting > 0 ? '#fa8c16' : undefined }} /></Col>
        <Col span={6}><Statistic title={t('infer.avgLatency')} value={last.latency_ms} suffix="ms" precision={0} /></Col>
      </Row>
      <div style={{ marginTop: 12 }}>
        <ResponsiveContainer width="100%" height={240}>
          <LineChart data={data}>
            <XAxis dataKey="t" fontSize={11} />
            <YAxis yAxisId="l" domain={[0, 100]} fontSize={11} />
            <YAxis yAxisId="r" orientation="right" allowDecimals={false} fontSize={11} />
            <RTooltip /><Legend />
            <Line yAxisId="l" dataKey="kv" name={t('infer.kvCache')} stroke="#1677ff" dot={false} />
            <Line yAxisId="r" dataKey="running" name={t('infer.runReqs')} stroke="#52c41a" dot={false} />
            <Line yAxisId="r" dataKey="waiting" name={t('infer.waitReqs')} stroke="#fa8c16" dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

function CodeBlock({ code, onCopy }: { code: string, onCopy: (s: string) => void }) {
  return (
    <div style={{ position: 'relative' }}>
      <Button size="small" type="text" icon={<CopyOutlined />}
        style={{ position: 'absolute', top: 6, right: 6, zIndex: 1 }}
        onClick={() => onCopy(code)} />
      <pre style={{ background: '#0b1021', color: '#d6e2ff', padding: 12, borderRadius: 6,
        maxHeight: 320, overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap', margin: 0 }}>{code}</pre>
    </div>
  )
}
