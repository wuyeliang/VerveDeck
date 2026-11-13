import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, Drawer,
  Descriptions, message, Popconfirm, Card, Tabs, Switch, InputNumber as IN,
} from 'antd'
import { PlusOutlined, PoweroffOutlined, PlayCircleOutlined, FileTextOutlined } from '@ant-design/icons'
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

  const load = useCallback(() => {
    api.get('/api/inference/deployments', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])

  const openDetail = (id: number) => {
    api.get(`/api/inference/deployments/${id}`).then((r) => setDetail(r.data)).catch(() => {})
    setChat([])
  }
  useEffect(() => {
    if (!detail || detail.status !== 'running') return
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

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('infer.title')}</h3>
        <Button type="primary" icon={<PlusOutlined />} onClick={async () => {
          form.resetFields()
          form.setFieldsValue({ engine: 'vllm', quantization: 'none', tensor_parallel: 1, max_model_len: 8192, gpu_mem_util: 0.85, replicas: 1, enable_lora: false })
          try {
            const m = await api.get('/api/models', { params: { project_id: projectId } })
            const all = m.data || []
            setModels(all.filter((x: any) => x.status === 'ready' && (x.type === 'base' || x.type === 'merged')))
            setLoraModels(all.filter((x: any) => x.type === 'lora'))
          } catch { /* ignore */ }
          setOpen(true)
        }}>{t('infer.newDeploy')}</Button>
      </Space>
      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('infer.colModel'), dataIndex: 'model', width: 180, ellipsis: true },
          { title: t('infer.colEngine'), dataIndex: 'engine', width: 80, render: (v: string) => <Tag color={v === 'vllm' ? 'blue' : 'cyan'}>{v}</Tag> },
          { title: t('infer.colQuant'), dataIndex: 'quantization', width: 80 },
          { title: t('infer.colTpRep'), width: 80, render: (_: any, r: any) => `${r.tensor_parallel} / ${r.replicas}` },
          { title: t('infer.colLora'), width: 70, render: (_: any, r: any) => r.enable_lora ? <Tag color="purple">{r.lora_adapters?.length || 0}</Tag> : '-' },
          { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={ST[v]}>{v}</Tag> },
          {
            title: t('common.actions'), width: 220, render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" onClick={() => openDetail(r.id)}>{t('common.detail')}</Button>
                {r.status !== 'running' && r.status !== 'creating' &&
                  <Tooltip2 label={t('infer.start')} icon={<PlayCircleOutlined />} onClick={() => api.post(`/api/inference/deployments/${r.id}/start`).then(() => message.info(t('infer.startInfo')))} />}
                {r.status === 'running' &&
                  <Tooltip2 label={t('infer.stop')} icon={<PoweroffOutlined />} onClick={() => api.post(`/api/inference/deployments/${r.id}/stop`).then(() => message.success(t('infer.stopped')))} />}
                <Popconfirm title={t('infer.deleteConfirm')} onConfirm={() => api.delete(`/api/inference/deployments/${r.id}`).then(() => load())}>
                  <Button size="small" danger>{t('common.delete')}</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]} />

      <Modal title={t('infer.modalTitle')} open={open} onCancel={() => setOpen(false)} width={640}
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
          })
          message.success(t('infer.created'))
          setOpen(false); load()
        }}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('infer.deployName')} rules={[{ required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: t('infer.nameRule') }]}><Input placeholder={t('infer.namePh')} /></Form.Item>
          <Form.Item name="model_asset_id" label={t('infer.modelAsset')} rules={[{ required: true }]}>
            <Select options={models.map((m: any) => ({ value: m.id, label: `${m.name} (${m.type})` }))} />
          </Form.Item>
          <Space>
            <Form.Item name="engine" label={t('infer.engine')}><Select style={{ width: 130 }} options={[{ value: 'vllm', label: 'vLLM' }, { value: 'sglang', label: 'SGLang' }]} /></Form.Item>
            <Form.Item name="quantization" label={t('infer.quant')}><Select style={{ width: 120 }} options={['none', 'awq', 'gptq', 'int8'].map((q) => ({ value: q, label: q }))} /></Form.Item>
            <Form.Item name="tensor_parallel" label={t('infer.tp')}><InputNumber min={1} max={4} /></Form.Item>
            <Form.Item name="max_model_len" label={t('infer.maxLen')}><InputNumber min={1024} step={1024} /></Form.Item>
          </Space>
          <Space>
            <Form.Item name="gpu_mem_util" label={t('infer.memUtil')}><InputNumber step={0.05} min={0.3} max={0.95} /></Form.Item>
            <Form.Item name="replicas" label={t('infer.replicas')}><InputNumber min={1} max={4} /></Form.Item>
          </Space>
          <Form.Item name="lora_assets" label={t('infer.extraLora')}>
            <Select mode="multiple" options={loraModels.map((m: any) => ({ value: m.id, label: m.name }))} placeholder={t('common.optional')} />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer title={detail ? t('infer.detailTitleWith', { name: detail.name }) : t('infer.detailTitle')} width={720} open={!!detail} onClose={() => setDetail(null)}>
        {detail && (
          <Tabs items={[
            {
              key: 'info', label: t('infer.tabApi'), children: (
                <div>
                  <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}
                    items={[
                      { key: 1, label: t('common.status'), children: <Tag color={ST[detail.status]}>{detail.status}</Tag> },
                      { key: 2, label: t('infer.dEngine'), children: `${detail.engine} / ${detail.quantization}` },
                      { key: 3, label: t('infer.colModel'), children: detail.model },
                      { key: 4, label: t('infer.dMaxLen'), children: `${detail.max_model_len} / ${detail.gpu_mem_util}` },
                      { key: 5, label: t('infer.baseUrl'), children: <code>{location.origin}/v1/{detail.name}</code> },
                      { key: 6, label: t('infer.auth'), children: t('infer.authDesc') },
                    ]} />
                  <Card size="small" title={t('infer.example')} style={{ marginBottom: 12 }}>
                    <pre style={{ fontSize: 12, margin: 0 }}>{`curl ${location.origin}/v1/${detail.name}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "x-api-key: ofk_xxx" \\
  -d '{"model": "${detail.name}", "messages": [{"role": "user", "content": "hi"}]}'}`}</pre>
                  </Card>
                  {detail.error && <p style={{ color: '#cf1322', whiteSpace: 'pre-wrap' }}>{detail.error}</p>}
                </div>
              ),
            },
            {
              key: 'play', label: t('infer.tabPlay'), children: (
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

function Tooltip2({ label, icon, onClick }: any) {
  return <Button size="small" {...{ icon }} onClick={onClick}>{label}</Button>
}
