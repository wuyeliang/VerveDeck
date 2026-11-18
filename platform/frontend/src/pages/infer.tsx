import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, Drawer,
  Descriptions, message, Popconfirm, Card, Tabs, Row, Col, Statistic,
  Collapse, Switch, Tooltip,
} from 'antd'
import {
  PlusOutlined, PoweroffOutlined, PlayCircleOutlined, CopyOutlined,
  CodeOutlined, ThunderboltOutlined, EditOutlined, ReloadOutlined, ExclamationCircleOutlined,
} from '@ant-design/icons'
import { LineChart, Line, XAxis, YAxis, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'

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
  const [sel, setSel] = useState<number[]>([])
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [models, setModels] = useState<any[]>([])
  const [loraModels, setLoraModels] = useState<any[]>([])
  const [detail, setDetail] = useState<any>(null)
  const [logs, setLogs] = useState('')
  const [logRole, setLogRole] = useState<'r' | 'p' | 'd' | 'rt'>('r')
  const [logReplica, setLogReplica] = useState(0)
  const [chat, setChat] = useState<any[]>([])
  const [chatInput, setChatInput] = useState('')
  const [chatLoading, setChatLoading] = useState(false)
  const [addAda, setAddAda] = useState<any>(null)
  const [mon, setMon] = useState<any>(null)
  const [nodeName, setNodeName] = useState('')
  const [rdma, setRdma] = useState<any>({ present: false, devices: [] as string[] })
  // 推理重构：后端注册表 / 缓存服务 / 编辑弹窗 / 实例视角
  const [backends, setBackends] = useState<any[]>([])
  const [cacheServices, setCacheServices] = useState<any[]>([])
  const [editTarget, setEditTarget] = useState<any>(null)
  const [editForm] = Form.useForm()
  const [inst, setInst] = useState<any>(null)
  const tp = Form.useWatch('tensor_parallel', form) || 1
  const rep = Form.useWatch('replicas', form) || 1
  const eng = Form.useWatch('engine', form) || 'vllm'
  const pdMode = Form.useWatch('pd_mode', form) || 'off'
  const prefillReplicas = Form.useWatch('prefill_replicas', form) || 1
  const prefillTp = Form.useWatch('prefill_tp', form) || 1
  const decodeReplicas = Form.useWatch('decode_replicas', form) || 1
  const decodeTp = Form.useWatch('decode_tp', form) || 1
  // GPU 需求预估：协同 = TP×副本；PD = P实例×P_TP + D实例×D_TP
  const gpuNeed = pdMode === 'pd' ? prefillReplicas * prefillTp + decodeReplicas * decodeTp : tp * rep
  // 创建表单：后端选择联动（custom 后端 → 版本；PD 互斥提示）
  const selBackend = Form.useWatch('backend_name', form) || ''
  const selBackendVer = Form.useWatch('backend_version', form)
  const selBackendObj = backends.find((b: any) => b.name === selBackend)
  // 编辑表单：后端选择联动（同创建口径）
  const editBackend = Form.useWatch('backend_name', editForm) || ''
  const editBackendVer = Form.useWatch('backend_version', editForm)
  const editBackendObj = backends.find((b: any) => b.name === editBackend)
  const editEngine = Form.useWatch('engine', editForm) || 'vllm'

  // 后端注册表 / 缓存服务（表单选择用；创建/编辑弹窗打开时刷新）
  const loadRefs = useCallback(() => {
    api.get('/api/inference/backends').then((r) => setBackends(r.data || [])).catch(() => {})
    api.get('/api/cache/services').then((r) => setCacheServices(r.data || [])).catch(() => {})
  }, [])
  useEffect(() => { loadRefs() }, [loadRefs])

  const load = useCallback(() => {
    api.get('/api/inference/deployments', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])
  const batchDel = useBatchDelete('/api/inference/deployments/batch-delete', () => { setSel([]); load() })
  // 节点名 + RDMA 状态（当前单节点集群，用于列表「节点」列 / 表单 placement 展示 / RDMA 徽标）
  useEffect(() => {
    api.get('/api/cluster/nodes').then((r) => {
      const n = r.data?.[0]
      if (!n) return
      setNodeName(n.name || '')
      setRdma(n.rdma || { present: false, devices: [] })
    }).catch(() => {})
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
    const fetchLog = () => api.get(`/api/inference/deployments/${detail.id}/logs`,
      { params: { tail: 80, role: logRole, replica: logReplica } })
      .then((r) => setLogs(r.data.logs)).catch(() => {})
    fetchLog()
    const t = setInterval(() => {
      fetchLog()
      api.get(`/api/inference/deployments/${detail.id}`).then((r) => setDetail(r.data)).catch(() => {})
    }, 4000)
    return () => clearInterval(t)
  }, [detail?.id, logRole, logReplica])

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

  // 实例视角（单实例存活 / 自动重启退避）：详情打开时 4s 轮询
  useEffect(() => {
    if (!detail) { setInst(null); return }
    let stop = false
    const loadInst = () => api.get(`/api/inference/deployments/${detail.id}/instances`)
      .then((r) => { if (!stop) setInst(r.data) }).catch(() => {})
    loadInst()
    const x = setInterval(loadInst, 4000)
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
        model: detail.served_model_name || detail.name, messages: msgs, stream: false,
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
    form.setFieldsValue({
      engine: 'vllm', quantization: 'none', tensor_parallel: 1, max_model_len: 8192,
      gpu_mem_util: 0.85, replicas: 1,
      pd_mode: 'off', prefill_replicas: 1, prefill_tp: 1, decode_replicas: 1, decode_tp: 1,
      kv_connector: 'nixl', node_count: 1,
    })
    try {
      const m = await api.get('/api/models', { params: { project_id: projectId } })
      const all = m.data || []
      setModels(all.filter((x: any) => x.status === 'ready' && (x.type === 'base' || x.type === 'merged')))
      setLoraModels(all.filter((x: any) => x.type === 'lora'))
    } catch { /* ignore */ }
    setOpen(true)
  }

  // 编辑部署（即时字段直改 / 参数字段自动重建；后端判定并回写状态）
  const openEdit = (d: any) => {
    loadRefs()
    editForm.resetFields()
    editForm.setFieldsValue({
      name: d.name, description: d.description || '',
      restart_on_error: d.restart_on_error !== false,
      backend_name: d.backend_name || '', backend_version: d.backend_version || '',
      cache_service_id: d.cache_service_id || undefined,
      engine: d.engine, quantization: d.quantization,
      tensor_parallel: d.tensor_parallel, replicas: d.replicas,
      max_model_len: d.max_model_len, gpu_mem_util: d.gpu_mem_util,
      kv_cpu_offload_gb: d.kv_cache_ext?.cpu_offload_gb ?? undefined,
      kv_hicache_size: d.kv_cache_ext?.hicache_size ?? undefined,
    })
    setEditTarget(d)
  }

  const submitEdit = async () => {
    if (!editTarget) return
    const v = await editForm.validateFields()
    const kv: Record<string, number> = {}
    if (v.engine === 'sglang') {
      if (v.kv_hicache_size != null && v.kv_hicache_size !== '') kv.hicache_size = v.kv_hicache_size
    } else if (v.kv_cpu_offload_gb != null && v.kv_cpu_offload_gb !== '') {
      kv.cpu_offload_gb = v.kv_cpu_offload_gb
    }
    // 清除语义：空串/undefined → null（后端 None 过滤前特判外键）
    const payload: Record<string, any> = {
      name: v.name, description: v.description || '',
      restart_on_error: !!v.restart_on_error,
      backend_name: v.backend_name || null, backend_version: v.backend_version || null,
      cache_service_id: v.cache_service_id ?? null,
      engine: v.engine, quantization: v.quantization,
      tensor_parallel: v.tensor_parallel, replicas: v.replicas,
      max_model_len: v.max_model_len, gpu_mem_util: v.gpu_mem_util,
      kv_cache_ext: kv,
    }
    await api.put(`/api/inference/deployments/${editTarget.id}`, payload)
    message.success(t('infer.edited'))
    setEditTarget(null)
    load()
  }

  // 单实例重建（部署级状态不变）
  const rebuildInst = (it: any) => {
    const roleMap: Record<string, string> = { prefill: 'p', decode: 'd', router: 'rt', replica: 'r' }
    api.post(`/api/inference/deployments/${detail.id}/instances/rebuild`,
      { role: roleMap[it.role] || 'r', index: it.index })
      .then(() => message.success(t('infer.rebuildSent')))
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  // 自动重启开关（即时字段；关闭时后端取消已排定的退避重启）
  const toggleRestart = (checked: boolean) => {
    api.put(`/api/inference/deployments/${detail.id}`, { restart_on_error: checked })
      .then(() => { setInst((s: any) => ({ ...s, restart_on_error: checked })); load() })
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  // 服务调用代码示例
  // 路径按部署名路由（/v1/<部署名>），但请求体 model 字段用引擎实际模型名（served_model_name，默认=部署名）
  const base = detail ? `${location.origin}/v1/${detail.name}` : ''
  const servedName = detail?.served_model_name || detail?.name || ''
  const codeCurl = `curl ${base}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "x-api-key: <PROJECT_API_KEY>" \\
  -d '{"model": "${servedName}", "messages": [{"role": "user", "content": "你好"}]}'`
  const codePy = `from openai import OpenAI

client = OpenAI(
    base_url="${base}",
    api_key="<PROJECT_API_KEY>",   # 或传 JWT 并改用 authorization
)
resp = client.chat.completions.create(
    model="${servedName}",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)`
  const codeNode = `const OpenAI = require("openai");

const client = new OpenAI({
  baseURL: "${base}",
  apiKey: "<PROJECT_API_KEY>",
});
const resp = await client.chat.completions.create({
  model: "${servedName}",
  messages: [{ role: "user", content: "你好" }],
});
console.log(resp.choices[0].message.content)`

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('infer.title')}</h3>
        <Space>
          {sel.length > 0 && <Button danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button type="primary" icon={<PlusOutlined />} onClick={openWizard}>{t('infer.newDeploy')}</Button>
        </Space>
      </Space>
      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }} scroll={{ x: 1280 }}
        rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }}
        columns={[
          { title: t('common.name'), dataIndex: 'name', width: 165, ellipsis: true, fixed: 'left' },
          { title: t('infer.colModel'), dataIndex: 'model', width: 150, ellipsis: true },
          { title: t('infer.colEngine'), dataIndex: 'engine', width: 130, render: (v: string, r: any) => (
            <Space size={4} wrap>
              <Tag color={v === 'vllm' ? 'blue' : 'cyan'}>{v}</Tag>
              {r.backend_name && <Tag color="orange">{r.backend_name}</Tag>}
              {r.pd_mode === 'pd' && <Tag color="volcano">PD</Tag>}
              {r.cache_service_id && <Tooltip title={t('infer.cacheOn')}><ThunderboltOutlined style={{ color: '#13c2c2' }} /></Tooltip>}
            </Space>
          ) },
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
          { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string, r: any) => (
            <Space size={4}>
              <Tag color={ST[v]}>{v}</Tag>
              {r.state_message && <Tooltip title={r.state_message}><ExclamationCircleOutlined style={{ color: v === 'failed' ? '#cf1322' : '#fa8c16' }} /></Tooltip>}
            </Space>
          ) },
          {
            title: t('common.actions'), width: 250, render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" onClick={() => openDetail(r.id)}>{t('common.detail')}</Button>
                <Button size="small" icon={<EditOutlined />} onClick={() => openEdit(r)}>{t('common.edit')}</Button>
                {r.status !== 'running' && r.status !== 'creating' &&
                  <Button size="small" icon={<PlayCircleOutlined />} onClick={() => api.post(`/api/inference/deployments/${r.id}/start`).then(() => message.info(t('infer.startInfo')))}>{t('infer.start')}</Button>}
                {r.status === 'running' &&
                  <Popconfirm title={t('infer.stopConfirm')} onConfirm={() => api.post(`/api/inference/deployments/${r.id}/stop`).then(() => message.success(t('infer.stopped')))} okButtonProps={{ danger: true }}>
                    <Button size="small" danger icon={<PoweroffOutlined />}>{t('infer.stop')}</Button>
                  </Popconfirm>}
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
          // 高级参数：留空=引擎默认（存量行为不变）；按引擎取对应字段，extra_args 原始透传
          const adv: Record<string, any> = {}
          if (v.engine === 'vllm') {
            if (v.adv_max_num_seqs != null && v.adv_max_num_seqs !== '') adv.max_num_seqs = v.adv_max_num_seqs
            if (v.adv_dtype) adv.dtype = v.adv_dtype
            if (v.adv_enforce_eager) adv.enforce_eager = true
            if (v.adv_prefix_caching) adv.enable_prefix_caching = true
            if (v.adv_chunked_prefill) adv.enable_chunked_prefill = true
            if (v.adv_served_model_name?.trim()) adv.served_model_name = v.adv_served_model_name.trim()
            if (v.adv_api_key?.trim()) adv.api_key = v.adv_api_key.trim()
          } else {
            if (v.adv_mem_fraction_static != null && v.adv_mem_fraction_static !== '') adv.mem_fraction_static = v.adv_mem_fraction_static
            if (v.adv_disable_cuda_graph) adv.disable_cuda_graph = true
          }
          if (v.adv_extra_args?.trim()) adv.extra_args = v.adv_extra_args.trim()
          // 扩展 KV 缓存加速（vLLM=CPU 卸载 GB / SGLang=HiCache 层大小 GB）
          const kv: Record<string, number> = {}
          if (v.engine === 'sglang') {
            if (v.adv_hicache_size != null && v.adv_hicache_size !== '') kv.hicache_size = v.adv_hicache_size
          } else if (v.adv_cpu_offload_gb != null && v.adv_cpu_offload_gb !== '') {
            kv.cpu_offload_gb = v.adv_cpu_offload_gb
          }
          // NCCL/UCX/NIXL 环境变量覆盖（可选 JSON 对象；空=引擎默认）
          let ncclEnv: Record<string, string> = {}
          if (v.adv_nccl_env?.trim()) {
            try {
              const o = JSON.parse(v.adv_nccl_env)
              if (o && typeof o === 'object' && !Array.isArray(o)) {
                ncclEnv = Object.fromEntries(Object.entries(o).map(([k, val]) => [k, String(val)]))
              }
            } catch { message.error(t('infer.ncclEnvInvalid')); return }
          }
          await api.post('/api/inference/deployments', {
            project_id: projectId, name: v.name, model_asset_id: v.model_asset_id,
            engine: v.engine, quantization: v.quantization, tensor_parallel: v.tensor_parallel,
            max_model_len: v.max_model_len, gpu_mem_util: v.gpu_mem_util,
            enable_lora: adapters.length > 0, lora_adapters: adapters, replicas: v.replicas,
            description: v.description || '', advanced: adv,
            pd_mode: v.pd_mode || 'off',
            prefill_replicas: v.prefill_replicas || 1, prefill_tp: v.prefill_tp || 1,
            decode_replicas: v.decode_replicas || 1, decode_tp: v.decode_tp || 1,
            kv_connector: v.kv_connector || 'nixl', node_count: v.node_count || 1,
            nccl_env: ncclEnv,
            // 推理重构：后端注册表 / 缓存加速 / 扩展 KV
            backend_name: v.backend_name || null, backend_version: v.backend_version || null,
            cache_service_id: v.cache_service_id ?? null,
            kv_cache_ext: kv,
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
                  {(selBackendObj && selBackendObj.source === 'custom'
                    ? (selBackendObj.versions.find((v: any) => v.version === selBackendVer)?.image
                      || selBackendObj.current_image)
                    : ENGINE_IMAGES[eng]) || '—'}
                </code>
              </Form.Item>
            </Space>
            {/* 推理配置：后端注册表（builtin 引擎 / 自定义后端）+ 版本 + 缓存加速服务 */}
            <Space wrap align="start" style={{ marginTop: 8 }}>
              <Form.Item name="backend_name" label={t('infer.backend')} tooltip={t('infer.backendHint')}>
                <Select style={{ width: 240 }} allowClear showSearch optionFilterProp="label"
                  placeholder={t('infer.backendDefault')}
                  onChange={(v) => {
                    if (v) form.setFieldsValue({ backend_version: '' })
                    if (v && backends.find((b: any) => b.name === v)?.source === 'custom'
                      && form.getFieldValue('pd_mode') === 'pd') {
                      form.setFieldsValue({ pd_mode: 'off' })  // 自定义后端不支持 PD 分离
                    }
                  }}
                  options={[
                    ...backends.filter((b: any) => b.source === 'builtin').map((b: any) => ({
                      value: '', label: t('infer.backendDefault'), disabled: true,
                    })),
                    ...backends.filter((b: any) => b.source === 'custom').map((b: any) => ({
                      value: b.name, label: b.name,
                    })),
                  ]} />
              </Form.Item>
              {selBackendObj && selBackendObj.source === 'custom' && (
                <Form.Item name="backend_version" label={t('infer.backendVer')} tooltip={t('infer.backendVerHint')}>
                  <Select style={{ width: 180 }} allowClear placeholder={t('infer.backendVerDefault')}
                    options={selBackendObj.versions.map((v: any) => ({
                      value: v.version,
                      label: v.is_default ? `${v.version} (${t('infer.defaultVer')})` : v.version,
                    }))} />
                </Form.Item>
              )}
              <Form.Item name="cache_service_id" label={t('infer.cacheSvc')} tooltip={t('infer.cacheSvcHint')}>
                <Select style={{ width: 200 }} allowClear showSearch optionFilterProp="label"
                  placeholder={t('infer.cacheSvcNone')}
                  options={cacheServices.map((c: any) => ({ value: c.id, label: c.name }))} />
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
              <ThunderboltOutlined />{' '}
              {pdMode === 'pd'
                ? t('infer.gpuNeedPd', { p: prefillReplicas, pt: prefillTp, d: decodeReplicas, dt: decodeTp, n: gpuNeed })
                : t('infer.gpuNeed', { n: gpuNeed, tp, r: rep })}
            </div>
          </InferSection>
          <InferSection title={t('infer.secTopology')}>
            <Space wrap align="start">
              <Form.Item name="pd_mode" label={t('infer.pdMode')}
                tooltip={selBackendObj && selBackendObj.source === 'custom'
                  ? t('infer.pdDisabledCustom') : t('infer.pdModeHint')}>
                <Select style={{ width: 200 }}
                  disabled={!!selBackendObj && selBackendObj.source === 'custom'}
                  options={[
                    { value: 'off', label: t('infer.pdOff') },
                    { value: 'pd', label: t('infer.pdOn') },
                  ]} />
              </Form.Item>
              <Form.Item label={t('infer.rdmaStatus')} tooltip={t('infer.rdmaStatusHint')}>
                <span>
                  {rdma.present
                    ? <Tag color="green">{t('infer.rdmaOn', { d: rdma.devices.join(', ') })}</Tag>
                    : <Tag>{t('infer.rdmaOff')}</Tag>}
                </span>
              </Form.Item>
            </Space>
            {pdMode === 'pd' && (
              <>
                <Space wrap align="start">
                  <Form.Item name="prefill_replicas" label={t('infer.prefillReplicas')} tooltip={t('infer.prefillReplicasHint')}><InputNumber min={1} max={8} /></Form.Item>
                  <Form.Item name="prefill_tp" label={t('infer.prefillTp')}><InputNumber min={1} max={8} /></Form.Item>
                  <Form.Item name="decode_replicas" label={t('infer.decodeReplicas')} tooltip={t('infer.decodeReplicasHint')}><InputNumber min={1} max={8} /></Form.Item>
                  <Form.Item name="decode_tp" label={t('infer.decodeTp')}><InputNumber min={1} max={8} /></Form.Item>
                  <Form.Item name="kv_connector" label={t('infer.kvConnector')} tooltip={t('infer.kvConnectorHint')}>
                    <Select style={{ width: 140 }} options={[
                      { value: 'nixl', label: 'NIXL' }, { value: 'mooncake', label: 'Mooncake' },
                    ]} />
                  </Form.Item>
                </Space>
                <div style={{ color: '#888', fontSize: 12, marginTop: -4 }}>
                  <ThunderboltOutlined /> {t('infer.gpuNeedPd', { p: prefillReplicas, pt: prefillTp, d: decodeReplicas, dt: decodeTp, n: gpuNeed })}
                </div>
              </>
            )}
            <Space wrap align="start" style={{ marginTop: pdMode === 'pd' ? 8 : 0 }}>
              <Form.Item name="node_count" label={t('infer.nodeCount')} tooltip={t('infer.nodeCountHint')}><InputNumber min={1} max={8} /></Form.Item>
              <Form.Item name="adv_nccl_env" label={t('infer.ncclEnv')} tooltip={t('infer.ncclEnvHint')}
                style={{ flex: 1, minWidth: 240, marginBottom: 0 }}>
                <Input.TextArea placeholder={'{"NCCL_IB_DISABLE": "0"}'} autoSize={{ minRows: 1, maxRows: 2 }} style={{ width: '100%' }} />
              </Form.Item>
            </Space>
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
          <Collapse ghost size="small" style={{ marginTop: 4, background: 'transparent' }} items={[{
            key: 'adv',
            label: <span style={{ fontWeight: 600 }}>{t('infer.advTitle')}</span>,
            children: (
              <div>
                <div style={{ color: '#999', fontSize: 12, marginBottom: 8 }}>{t('infer.advHint')}</div>
                {eng === 'vllm' ? (
                  <>
                    <Row gutter={12}>
                      <Col span={8}>
                        <Form.Item name="adv_max_num_seqs" label={t('infer.advMaxNumSeqs')}>
                          <InputNumber min={1} step={1} placeholder={t('infer.advDefault')} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item name="adv_dtype" label={t('infer.advDtype')}>
                          <Select allowClear placeholder={t('infer.advDefault')} options={[
                            { value: 'auto', label: 'auto' }, { value: 'bf16', label: 'bf16' }, { value: 'fp16', label: 'fp16' },
                          ]} />
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item name="adv_served_model_name" label={t('infer.advServedName')}>
                          <Input placeholder={t('infer.advDefault')} maxLength={120} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={12}>
                      <Col span={8}><Form.Item name="adv_enforce_eager" label={t('infer.advEnforceEager')} valuePropName="checked"><Switch /></Form.Item></Col>
                      <Col span={8}><Form.Item name="adv_prefix_caching" label={t('infer.advPrefixCaching')} valuePropName="checked"><Switch /></Form.Item></Col>
                      <Col span={8}><Form.Item name="adv_chunked_prefill" label={t('infer.advChunkedPrefill')} valuePropName="checked"><Switch /></Form.Item></Col>
                    </Row>
                    <Form.Item name="adv_api_key" label={t('infer.advApiKey')}>
                      <Input.Password placeholder={t('infer.advDefault')} autoComplete="off" maxLength={128} />
                    </Form.Item>
                  </>
                ) : (
                  <Row gutter={12}>
                    <Col span={12}>
                      <Form.Item name="adv_mem_fraction_static" label={t('infer.advMemFraction')}>
                        <InputNumber step={0.05} min={0.3} max={0.95} placeholder={t('infer.advDefault')} style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col span={12}><Form.Item name="adv_disable_cuda_graph" label={t('infer.advDisableCudaGraph')} valuePropName="checked"><Switch /></Form.Item></Col>
                  </Row>
                )}
                {/* 扩展 KV 缓存加速（部署级）：vLLM CPU 卸载 / SGLang HiCache 层级 */}
                <Space wrap align="start" style={{ marginTop: 8 }}>
                  {eng === 'sglang' ? (
                    <Form.Item name="adv_hicache_size" label={t('infer.hicacheSize')} tooltip={t('infer.hicacheSizeHint')}>
                      <InputNumber min={1} max={512} step={1} placeholder={t('infer.advDefault')} addonAfter="GB" style={{ width: 180 }} />
                    </Form.Item>
                  ) : (
                    <Form.Item name="adv_cpu_offload_gb" label={t('infer.cpuOffloadGb')} tooltip={t('infer.cpuOffloadGbHint')}>
                      <InputNumber min={1} max={256} step={1} placeholder={t('infer.advDefault')} addonAfter="GB" style={{ width: 180 }} />
                    </Form.Item>
                  )}
                </Space>
                <Form.Item name="adv_extra_args" label={t('infer.advExtraArgs')} tooltip={t('infer.advExtraArgsHint')} style={{ marginTop: 8 }}>
                  <Input.TextArea placeholder={t('infer.advExtraArgsPh')} autoSize={{ minRows: 1, maxRows: 3 }} />
                </Form.Item>
              </div>
            ),
          }]} />
        </Form>
      </Modal>

      {/* 编辑部署：即时字段（名/描述/自动重启/后端）直改；参数字段触发实例重建（后端判定） */}
      <Modal title={editTarget ? `${t('infer.editTitle')} · ${editTarget.name}` : t('infer.editTitle')}
        open={!!editTarget} onCancel={() => setEditTarget(null)} width={680}
        onOk={async () => {
          const v = await editForm.validateFields()
          // PD 部署不走协同 TP/副本参数（编辑面板未暴露 P/D 角色参数，禁改防误操作）
          if (editTarget.pd_mode === 'pd' &&
              (v.tensor_parallel !== editTarget.tensor_parallel || v.replicas !== editTarget.replicas)) {
            message.error(t('infer.pdEditNotAllowed')); return
          }
          await submitEdit()
        }}>
        <Form form={editForm} layout="vertical">
          <InferSection title={t('infer.secBasic')}>
            <Form.Item name="name" label={t('infer.deployName')}
              rules={[{ required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: t('infer.nameRule') }]}>
              <Input maxLength={60} showCount />
            </Form.Item>
            <Form.Item name="description" label={t('infer.desc')}>
              <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
            </Form.Item>
            <Form.Item name="restart_on_error" label={t('infer.restartOn')} valuePropName="checked"
              tooltip={t('infer.restartHint')}>
              <Switch />
            </Form.Item>
          </InferSection>
          <InferSection title={t('infer.secBackend')}>
            <Space wrap align="start">
              <Form.Item name="backend_name" label={t('infer.backend')} tooltip={t('infer.backendHint')}>
                <Select style={{ width: 240 }} allowClear showSearch optionFilterProp="label"
                  placeholder={t('infer.backendDefault')}
                  onChange={(v) => {
                    if (v) editForm.setFieldsValue({ backend_version: '' })
                    if (v && backends.find((b: any) => b.name === v)?.source === 'custom'
                      && editTarget?.pd_mode === 'pd') {
                      message.warning(t('infer.customNoPd'))
                    }
                  }}
                  options={backends.filter((b: any) => b.source === 'custom')
                    .map((b: any) => ({ value: b.name, label: b.name }))} />
              </Form.Item>
              {editBackendObj && (
                <Form.Item name="backend_version" label={t('infer.backendVer')} tooltip={t('infer.backendVerHint')}>
                  <Select style={{ width: 180 }} allowClear placeholder={t('infer.backendVerDefault')}
                    options={editBackendObj.versions.map((v: any) => ({
                      value: v.version,
                      label: v.is_default ? `${v.version} (${t('infer.defaultVer')})` : v.version,
                    }))} />
                </Form.Item>
              )}
              <Form.Item name="cache_service_id" label={t('infer.cacheSvc')} tooltip={t('infer.cacheSvcHint')}>
                <Select style={{ width: 200 }} allowClear showSearch optionFilterProp="label"
                  placeholder={t('infer.cacheSvcNone')}
                  options={cacheServices.map((c: any) => ({ value: c.id, label: c.name }))} />
              </Form.Item>
            </Space>
            <div style={{ color: '#999', fontSize: 12, marginTop: -4 }}>{t('infer.editRebuildHint')}</div>
          </InferSection>
          <InferSection title={t('infer.secParams')}>
            <Space wrap>
              <Form.Item name="engine" label={t('infer.engine')}
                rules={[{ required: true }]}>
                <Select style={{ width: 130 }} options={[{ value: 'vllm', label: 'vLLM' }, { value: 'sglang', label: 'SGLang' }]} />
              </Form.Item>
              <Form.Item name="quantization" label={t('infer.quant')}>
                <Select style={{ width: 120 }} options={['none', 'awq', 'gptq', 'int8'].map((q) => ({ value: q, label: q }))} />
              </Form.Item>
              <Form.Item name="tensor_parallel" label={t('infer.tp')}><InputNumber min={1} max={8} /></Form.Item>
              <Form.Item name="replicas" label={t('infer.replicas')}><InputNumber min={1} max={8} /></Form.Item>
              <Form.Item name="max_model_len" label={t('infer.maxLen')}><InputNumber min={1024} step={1024} /></Form.Item>
              <Form.Item name="gpu_mem_util" label={t('infer.memUtil')}><InputNumber step={0.05} min={0.3} max={0.95} /></Form.Item>
            </Space>
            <Space wrap align="start" style={{ marginTop: 8 }}>
              {editEngine === 'sglang' ? (
                <Form.Item name="kv_hicache_size" label={t('infer.hicacheSize')} tooltip={t('infer.hicacheSizeHint')}>
                  <InputNumber min={1} max={512} step={1} placeholder={t('infer.advDefault')} addonAfter="GB" style={{ width: 180 }} />
                </Form.Item>
              ) : (
                <Form.Item name="kv_cpu_offload_gb" label={t('infer.cpuOffloadGb')} tooltip={t('infer.cpuOffloadGbHint')}>
                  <InputNumber min={1} max={256} step={1} placeholder={t('infer.advDefault')} addonAfter="GB" style={{ width: 180 }} />
                </Form.Item>
              )}
            </Space>
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
                      {
                        key: 'topo', label: t('infer.topoDetail'),
                        children: detail.pd_mode === 'pd'
                          ? (
                            <Space size={4} wrap>
                              <Tag color="volcano">{t('infer.pdOn')}</Tag>
                              <Tag>P×{detail.prefill_replicas}·TP{detail.prefill_tp}</Tag>
                              <Tag>D×{detail.decode_replicas}·TP{detail.decode_tp}</Tag>
                              <Tag color="blue">{detail.kv_connector}</Tag>
                              <Tag>{t('infer.gpuNeedShort', { n: detail.gpu_need })}</Tag>
                            </Space>
                          )
                          : <Space size={4}><Tag>{t('infer.pdOff')}</Tag><Tag>{t('infer.gpuNeedShort', { n: detail.gpu_need })}</Tag></Space>,
                      },
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
                  {detail.state_message && detail.status !== 'running' && (
                    <p style={{ color: '#fa8c16', whiteSpace: 'pre-wrap', marginTop: -4 }}>
                      <ExclamationCircleOutlined style={{ marginRight: 6 }} />{detail.state_message}
                    </p>
                  )}
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
              key: 'instances', label: <span><ReloadOutlined /> {t('infer.tabInstances')}</span>, children: (
                <div>
                  <Space wrap style={{ marginBottom: 12 }}>
                    <Switch checked={inst?.restart_on_error !== false} onChange={toggleRestart}
                      checkedChildren={t('infer.restartOn')} unCheckedChildren={t('infer.restartOff')} />
                    <span style={{ color: '#888', fontSize: 12 }}>{t('infer.restartHint')}</span>
                    {inst && inst.error_count > 0 && (
                      <Tag color="red">{t('infer.errCount', { n: inst.error_count })}</Tag>
                    )}
                    {inst && inst.next_restart_at && (
                      <Tag color="orange">{t('infer.nextRestart', { at: inst.next_restart_at })}</Tag>
                    )}
                  </Space>
                  {inst?.state_message && (
                    <p style={{ color: '#888', fontSize: 12, marginTop: 0, marginBottom: 8 }}>
                      <ExclamationCircleOutlined style={{ marginRight: 6 }} />{inst.state_message}
                    </p>
                  )}
                  <Table size="small" rowKey={(r: any) => `${r.role}-${r.index}`} pagination={false}
                    dataSource={inst?.instances || []}
                    columns={[
                      { title: t('infer.instRole'), dataIndex: 'role', width: 100, render: (v: string) => (
                        <Tag color={v === 'prefill' ? 'orange' : v === 'decode' ? 'green' : v === 'router' ? 'purple' : 'blue'}>{v}</Tag>
                      ) },
                      { title: t('infer.instIndex'), dataIndex: 'index', width: 70 },
                      { title: t('common.name'), dataIndex: 'name', ellipsis: true },
                      { title: t('infer.instAlive'), dataIndex: 'alive', width: 90, render: (v: boolean) => (
                        <Tag color={v ? 'success' : 'error'}>{v ? t('infer.alive') : t('infer.dead')}</Tag>
                      ) },
                      { title: t('infer.instIp'), dataIndex: 'ip', width: 140, render: (v: string) => v ? <code style={{ fontSize: 11 }}>{v}</code> : <span style={{ color: '#bbb' }}>-</span> },
                      { title: t('common.actions'), width: 90, render: (_: any, it: any) => (
                        <Popconfirm title={t('infer.rebuildConfirm', { name: it.name })} okButtonProps={{ danger: true }}
                          onConfirm={() => rebuildInst(it)}>
                          <Button size="small" icon={<ReloadOutlined />}>{t('infer.rebuild')}</Button>
                        </Popconfirm>
                      ) },
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
                        <Popconfirm title={t('infer.unloadConfirm', { name: r.name })} okButtonProps={{ danger: true }}
                          onConfirm={async () => { await api.delete(`/api/inference/deployments/${detail.id}/adapters/${r.name}`); openDetail(detail.id) }}>
                          <Button size="small" danger>{t('infer.unload')}</Button>
                        </Popconfirm>
                      )},
                    ]} />
                </div>
              ),
            },
            {
              key: 'logs', label: t('infer.tabLogs'), children: (
                <div>
                  {detail.pd_mode === 'pd' ? (
                    <Space style={{ marginBottom: 8 }} wrap>
                      <Select size="small" value={logRole} style={{ width: 200 }}
                        onChange={(v) => { setLogRole(v); setLogReplica(0) }}
                        options={(
                          [
                            { value: 'p', label: t('infer.logPrefill', { n: detail.prefill_replicas }) },
                            { value: 'd', label: t('infer.logDecode', { n: detail.decode_replicas }) },
                            ...(detail.engine === 'sglang' ? [{ value: 'rt', label: t('infer.logRouter') }] : []),
                          ] as any[]
                        )} />
                      {logRole !== 'rt' && (
                        <InputNumber size="small" min={0} max={Math.max(0, (logRole === 'p' ? detail.prefill_replicas : detail.decode_replicas) - 1)}
                          value={logReplica} onChange={(v) => setLogReplica(v ?? 0)} style={{ width: 90 }}
                          prefix={t('infer.logReplica')} />
                      )}
                    </Space>
                  ) : null}
                  <pre style={{ background: '#f6f8fa', padding: 12, borderRadius: 6, maxHeight: 460, overflow: 'auto', fontSize: 12 }}>{logs || t('infer.noLogs')}</pre>
                </div>
              ),
            },
            {
              key: 'scale', label: t('infer.tabScale'), children: (
                detail.pd_mode === 'pd' ? (
                  <div>
                    <p style={{ color: '#888', fontSize: 12, marginTop: 0 }}>{t('infer.pdScaleHint')}</p>
                    {(['prefill', 'decode'] as const).map((role) => {
                      const cur = role === 'prefill' ? (detail.prefill_replicas || 1) : (detail.decode_replicas || 1)
                      const tpR = role === 'prefill' ? (detail.prefill_tp || 1) : (detail.decode_tp || 1)
                      const call = (replicas: number) => api.post(`/api/inference/deployments/${detail.id}/scale-role`, { role, replicas })
                        .then(() => message.success(t('infer.scaledRole')))
                        .catch((e) => message.error(e.response?.data?.detail || e.message))
                      return (
                        <Space key={role} style={{ marginBottom: 10 }}>
                          <Tag color={role === 'prefill' ? 'orange' : 'green'}>{role}</Tag>
                          <span>{t('infer.curRoleReplicas', { n: cur, tp: tpR })}</span>
                          <Button size="small" onClick={() => call(cur + 1)}>{t('infer.scaleUp')}</Button>
                          <Button size="small" disabled={cur <= 1} onClick={() => call(cur - 1)}>{t('infer.scaleDown')}</Button>
                        </Space>
                      )
                    })}
                  </div>
                ) : (
                  <Space>
                    <span>{t('infer.curReplicas', { n: detail.replicas })}</span>
                    <Button onClick={() => api.post(`/api/inference/deployments/${detail.id}/scale`, { replicas: detail.replicas + 1 }).then(() => message.success(t('infer.scaledUp')))}>{t('infer.scaleUp')}</Button>
                    <Button disabled={detail.replicas <= 1} onClick={() => api.post(`/api/inference/deployments/${detail.id}/scale`, { replicas: detail.replicas - 1 }).then(() => message.success(t('infer.scaledDown')))}>{t('infer.scaleDown')}</Button>
                  </Space>
                )
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
