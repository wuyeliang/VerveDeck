import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, Drawer,
  Descriptions, message, Popconfirm, Card, Tabs,
} from 'antd'
import {
  PlusOutlined, PoweroffOutlined, PlayCircleOutlined, CopyOutlined,
  CodeOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const ST: Record<string, string> = { creating: 'processing', running: 'success', stopped: 'default', failed: 'error' }

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
  const tp = Form.useWatch('tensor_parallel', form) || 1
  const rep = Form.useWatch('replicas', form) || 1

  const load = useCallback(() => {
    api.get('/api/inference/deployments', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])

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
      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name', width: 180, ellipsis: true },
          { title: t('infer.colModel'), dataIndex: 'model', width: 160, ellipsis: true },
          { title: t('infer.colEngine'), dataIndex: 'engine', width: 80, render: (v: string) => <Tag color={v === 'vllm' ? 'blue' : 'cyan'}>{v}</Tag> },
          { title: t('infer.colTpRep'), width: 80, render: (_: any, r: any) => `${r.tensor_parallel} / ${r.replicas}` },
          {
            title: t('infer.endpoint'), width: 190, render: (_: any, r: any) => r.status === 'running'
              ? <Space size={4}><code style={{ fontSize: 12 }}>/v1/{r.name}</code>
                <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copy(`${location.origin}/v1/${r.name}`)} /></Space>
              : <span style={{ color: '#bbb' }}>-</span>,
          },
          { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={ST[v]}>{v}</Tag> },
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
            <Space wrap>
              <Form.Item name="engine" label={t('infer.engine')}>
                <Select style={{ width: 130 }} options={[{ value: 'vllm', label: 'vLLM' }, { value: 'sglang', label: 'SGLang' }]} />
              </Form.Item>
              <Form.Item name="tensor_parallel" label={t('infer.tp')}><InputNumber min={1} max={8} /></Form.Item>
              <Form.Item name="replicas" label={t('infer.replicas')}><InputNumber min={1} max={8} /></Form.Item>
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
