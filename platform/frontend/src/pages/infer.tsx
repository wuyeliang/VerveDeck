import React, { useEffect, useState, useCallback, useMemo } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, Drawer,
  Descriptions, message, Popconfirm, Card, Tabs, Row, Col, Statistic,
  Switch, Tooltip, Dropdown, Collapse, AutoComplete,
} from 'antd'
import {
  PlusOutlined, PoweroffOutlined, PlayCircleOutlined, CopyOutlined,
  CodeOutlined, ThunderboltOutlined, EditOutlined, ReloadOutlined, ExclamationCircleOutlined,
  SearchOutlined, MoreOutlined, ProfileOutlined, DeleteOutlined,
  ArrowLeftOutlined, MinusCircleOutlined,
} from '@ant-design/icons'
import { LineChart, Line, XAxis, YAxis, Tooltip as RTooltip, ResponsiveContainer, Legend } from 'recharts'
import dayjs from 'dayjs'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'
import { StatusDot, STATUS_TONE, SectionTitle } from './infer-shared'

// 引擎镜像由平台级 pin 决定（与 compose IMAGES_VLLM/IMAGES_SGLANG 一致），按引擎展示
const ENGINE_IMAGES: Record<string, string> = {
  vllm: 'vllm/vllm-openai:v0.33.0',
  sglang: 'lmsysorg/sglang:v0.5.23',
}

// 部署页卡片分组（GPUWorks 式信息架构：分组卡片 + 字段下方说明文字）
const Group = ({ title, extra, children }: {
  title: string; extra?: React.ReactNode; children: React.ReactNode;
}) => (
  <Card size="small" title={<span style={{ fontSize: 13, fontWeight: 600 }}>{title}</span>}
    extra={extra} style={{ marginBottom: 12 }}>
    {children}
  </Card>
)

// 放置/运行环境字段（提交与预览共用口径）：TP 自动（0=估算）/ PP / 目标节点 /
// 标签约束 / 手动 GPU 卡号 / 自定义镜像 / Docker 参数 / 命令编辑（PD 模式置空）
function buildPlacement(v: any): Record<string, any> {
  return {
    tensor_parallel: v.tp_auto ? 0 : (v.tensor_parallel || 1),
    pipeline_parallel: v.pipeline_parallel || 1,
    node_name: v.node_target === 'manual' ? (v.node_name || null) : null,
    node_labels: (v.node_labels || []).map((x: any) => String(x).trim()).filter(Boolean),
    gpu_ids: v.gpu_select === 'manual'
      ? String(v.gpu_ids_str || '').split(/[,，\s]+/).filter(Boolean).map(Number)
        .filter((x: number) => !Number.isNaN(x))
      : [],
    image_ref: v.image_select === 'custom' ? String(v.image_ref || '').trim() : '',
    // 引擎版本（builtin vllm/sglang tag；空=跟随平台当前版本）
    engine_version: v.engine_version || '',
    docker_args: String(v.docker_args || '').split('\n').map((x: string) => x.trim()).filter(Boolean),
    cmd_override: (v.pd_mode || 'off') === 'pd' ? '' : String(v.cmd_override || '').trim(),
  }
}

// 启动配置预览：表单字段变化（防抖 500ms）调后端 /preview（与 build_cmd 同一命令拼装路径，
// 保证预览 == 实际启动命令），深色代码块渲染 镜像 / 容器环境变量 / 各实例启动命令（可复制）
function LaunchPreview({ form, tick, loraModels, t, onPreview }: {
  form: any; tick: number; loraModels: any[]; t: (k: string, o?: Record<string, any>) => string;
  onPreview?: (pv: any) => void;
}) {
  const [pv, setPv] = useState<any>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  // 上抛预览结果（tp_auto 估算 / 节点卡数与标签），供资源配置子 tab 与配置评估卡展示
  useEffect(() => { onPreview?.(pv) }, [pv, onPreview])
  useEffect(() => {
    if (!tick) return
    let stop = false
    setBusy(true)
    const h = setTimeout(async () => {
      const v = form.getFieldsValue(true) || {}
      // 组装口径与 submitCreate 完全一致（advanced/kv/nccl/lora）
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
      const kv: Record<string, number> = {}
      if (v.engine === 'sglang') {
        if (v.adv_hicache_size != null && v.adv_hicache_size !== '') kv.hicache_size = v.adv_hicache_size
      } else if (v.adv_cpu_offload_gb != null && v.adv_cpu_offload_gb !== '') {
        kv.cpu_offload_gb = v.adv_cpu_offload_gb
      }
      // KEY\VALUE 行 → 对象（与 submitCreate 同口径）
      const ncclEnv: Record<string, string> = {}
      ;(v.nccl_env_rows || []).forEach((r: any) => {
        const k = r?.key ? String(r.key).trim() : ''
        if (k) ncclEnv[k] = String(r?.value ?? '')
      })
      const adapters: any[] = []
      ;(v.lora_assets || []).forEach((aid: number) => {
        const a = loraModels.find((x: any) => x.id === aid)
        if (a) adapters.push({ name: a.name.replace(/[^a-zA-Z0-9_-]/g, '_'), path: a.path })
      })
      try {
        const r = await api.post('/api/inference/deployments/preview', {
          name: v.name || '', model_asset_id: v.model_asset_id ?? null,
          engine: v.engine || 'vllm', quantization: v.quantization || 'none',
          max_model_len: v.max_model_len || 8192,
          gpu_mem_util: v.gpu_mem_util ?? 0.85,
          enable_lora: adapters.length > 0, lora_adapters: adapters,
          advanced: adv,
          pd_mode: v.pd_mode || 'off',
          prefill_replicas: v.prefill_replicas || 1, prefill_tp: v.prefill_tp || 1,
          decode_replicas: v.decode_replicas || 1, decode_tp: v.decode_tp || 1,
          kv_connector: v.kv_connector || 'nixl', node_count: v.node_count || 1,
          nccl_env: ncclEnv,
          backend_name: v.backend_name || null, backend_version: v.backend_version || null,
          cache_service_id: v.cache_service_id ?? null,
          kv_cache_ext: kv,
          ...buildPlacement(v),
        })
        if (!stop) { setPv(r.data); setErr(''); setBusy(false) }
      } catch (e: any) {
        if (!stop) { setErr(e.response?.data?.detail || e.message); setBusy(false) }
      }
    }, 500)
    return () => { stop = true; clearTimeout(h) }
  }, [tick])

  const dark: React.CSSProperties = {
    margin: 0, padding: '10px 12px', background: '#1e1e2e', color: '#e2e2e8',
    borderRadius: 6, fontSize: 12, lineHeight: 1.7, whiteSpace: 'pre-wrap', wordBreak: 'break-all',
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  }
  const copyLine = (text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => message.success(t('infer.copied')),
      () => message.error(t('infer.copy')),
    )
  }
  if (!pv) {
    return <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12 }}>
      {busy ? t('infer.previewLoading') : t('infer.previewPhCmd')}
    </div>
  }
  return (
    <div>
      <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginBottom: 8 }}>{t('infer.previewDesc')}</div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', marginBottom: 8 }}>
        <Tag color="blue"><code style={{ fontSize: 12 }}>{pv.image || '—'}</code></Tag>
        {pv.commands.map((c: any) => (
          <Tag key={c.label}>{c.label} · {t('infer.gpusPer', { n: c.gpus })}</Tag>
        ))}
        <Button size="small" type="text" icon={<CopyOutlined />}
          onClick={() => copyLine(pv.commands.map((c: any) => c.cmd).join('\n\n'))}>
          {t('infer.copyAll')}
        </Button>
      </div>
      {Object.keys(pv.env || {}).length > 0 && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)', marginBottom: 4 }}>
            {t('infer.previewEnv')}
          </div>
          <pre style={dark}>{Object.entries(pv.env).map(([k, val]) => `${k}=${val}`).join('\n')}</pre>
        </div>
      )}
      {pv.commands.map((c: any) => (
        <div key={c.label} style={{ marginBottom: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
            <Tag color={c.label.startsWith('prefill') ? 'orange' : c.label.startsWith('decode') ? 'green' : 'blue'}>{c.label}</Tag>
            <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copyLine(c.cmd)}>
              {t('infer.copy')}
            </Button>
          </div>
          <pre style={dark}>{c.cmd}</pre>
        </div>
      ))}
      {err && <div style={{ color: '#cf1322', fontSize: 12 }}>{err}</div>}
    </div>
  )
}

export default function Inference() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [sel, setSel] = useState<number[]>([])
  const [kw, setKw] = useState('')
  const [stFilter, setStFilter] = useState('')
  const [view, setView] = useState<'list' | 'deploy'>('list')
  const [createTab, setCreateTab] = useState<'quick' | 'expert'>('quick')
  const [formTick, setFormTick] = useState(0)
  const [submitting, setSubmitting] = useState(false)
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
  const [nodeGpu, setNodeGpu] = useState<{ total: number; used: number }>({ total: 0, used: 0 })
  const [nodeLabels, setNodeLabels] = useState<string[]>([])
  const [previewRes, setPreviewRes] = useState<any>(null)  // 预览结果（自动选择结果：TP 估算 / 节点）
  // 引擎版本（builtin vllm/sglang 可选 tag；空=跟随平台当前版本）
  const [engVersions, setEngVersions] = useState<Record<string, { current: string; versions: string[] }>>({})
  // 推理重构：后端注册表 / 缓存服务 / 编辑抽屉 / 实例视角
  const [backends, setBackends] = useState<any[]>([])
  const [cacheServices, setCacheServices] = useState<any[]>([])
  const [editTarget, setEditTarget] = useState<any>(null)
  const [editForm] = Form.useForm()
  const [inst, setInst] = useState<any>(null)
  // 两级展开：行展开 = 实例子表（懒加载 /instances，展开期间 4s 轮询）
  const [expandedKeys, setExpandedKeys] = useState<number[]>([])
  const [rowInstances, setRowInstances] = useState<Record<number, any>>({})
  const tp = Form.useWatch('tensor_parallel', form) || 1
  const rep = Form.useWatch('replicas', form) || 1
  const eng = Form.useWatch('engine', form) || 'vllm'
  const pdMode = Form.useWatch('pd_mode', form) || 'off'
  const prefillReplicas = Form.useWatch('prefill_replicas', form) || 1
  const prefillTp = Form.useWatch('prefill_tp', form) || 1
  const decodeReplicas = Form.useWatch('decode_replicas', form) || 1
  const decodeTp = Form.useWatch('decode_tp', form) || 1
  // 部署放置/运行环境：PP / TP 自动 / 目标节点 / 标签约束 / GPU 选择 / 镜像选择
  const pp = Form.useWatch('pipeline_parallel', form) || 1
  const tpAuto = !!Form.useWatch('tp_auto', form)
  const nodeTarget = Form.useWatch('node_target', form) || 'auto'
  const nodeLabelsSel: string[] = Form.useWatch('node_labels', form) || []
  const gpuSelect = Form.useWatch('gpu_select', form) || 'auto'
  const imageSelect = Form.useWatch('image_select', form) || 'auto'
  // GPU 需求预估：协同 = TP×副本×PP；PD = (P实例×P_TP + D实例×D_TP)×PP
  const gpuNeed = (pdMode === 'pd'
    ? prefillReplicas * prefillTp + decodeReplicas * decodeTp
    : tp * rep) * pp
  // 标签约束匹配（本节点标签子集检查；与后端 preview 400 同口径的前置展示）
  const missingNodeLabels = nodeLabelsSel.filter((l) => !nodeLabels.includes(l))
  // 创建表单：后端选择联动（custom 后端 → 版本；PD 互斥提示）
  const selBackend = Form.useWatch('backend_name', form) || ''
  const selBackendVer = Form.useWatch('backend_version', form)
  const selBackendObj = backends.find((b: any) => b.name === selBackend)
  // 编辑表单：后端选择联动（同创建口径）
  const editBackend = Form.useWatch('backend_name', editForm) || ''
  const editBackendVer = Form.useWatch('backend_version', editForm)
  const editBackendObj = backends.find((b: any) => b.name === editBackend)
  const editEngine = Form.useWatch('engine', editForm) || 'vllm'
  // 引擎版本选项（空值=跟随平台当前版本；当前生效 tag 标「当前」）
  const evOptions = (e: string) => {
    const info = engVersions[e]
    return [
      { value: '', label: t('infer.engineVersionFollow', { v: info?.current || '—' }) },
      ...(info?.versions || []).map((v: string) => ({
        value: v, label: v === info?.current ? `${v} (${t('infer.curTag')})` : v,
      })),
    ]
  }

  // 状态文案（状态点统一走共享色板：绿=在服 琥珀=未就绪 红=失败 灰=停止）
  const stLabel = (s: string) => {
    const k = `infer.st${s.charAt(0).toUpperCase() + s.slice(1)}`
    return t(k) === k ? s : t(k)
  }

  // 后端注册表 / 缓存服务（表单选择用；页面打开时刷新）
  const loadRefs = useCallback(() => {
    api.get('/api/inference/backends').then((r) => setBackends(r.data || [])).catch(() => {})
    api.get('/api/cache/services').then((r) => setCacheServices(r.data || [])).catch(() => {})
    // 引擎版本列表（Docker Hub tags 前 10，后端缓存 10 分钟；失败不阻塞页面）
    ;(['vllm', 'sglang'] as const).forEach((e) => {
      api.get('/api/inference/engine-versions', { params: { engine: e } })
        .then((r) => setEngVersions((p) => ({ ...p, [e]: r.data }))).catch(() => {})
    })
  }, [])
  useEffect(() => { loadRefs() }, [loadRefs])

  const load = useCallback(() => {
    api.get('/api/inference/deployments', { params: { project_id: projectId } }).then((r) => {
      const data = r.data || []
      setList(data)
      // 行已删除（批量删除/单删后）清理展开态
      setExpandedKeys((k) => k.filter((id) => data.some((x: any) => x.id === id)))
    }).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const x = setInterval(load, 5000); return () => clearInterval(x) }, [load])
  const batchDel = useBatchDelete('/api/inference/deployments/batch-delete', () => { setSel([]); load() })

  // 展开行实例懒加载（展开期间 4s 轮询）
  const loadRowInstances = useCallback((ids: number[]) => {
    ids.forEach((id) => {
      api.get(`/api/inference/deployments/${id}/instances`).then((r) => {
        setRowInstances((m) => ({ ...m, [id]: r.data }))
      }).catch(() => {})
    })
  }, [])
  useEffect(() => {
    if (expandedKeys.length === 0) return
    loadRowInstances(expandedKeys)
    const x = setInterval(() => loadRowInstances(expandedKeys), 4000)
    return () => clearInterval(x)
  }, [expandedKeys, loadRowInstances])

  // 节点名 + RDMA 状态（当前单节点集群，用于列表「节点」列 / 表单 placement 展示 / RDMA 徽标）
  useEffect(() => {
    api.get('/api/cluster/nodes').then((r) => {
      const n = r.data?.[0]
      if (!n) return
      setNodeName(n.name || '')
      setRdma(n.rdma || { present: false, devices: [] })
      setNodeGpu({ total: n.gpu_count || 0, used: n.gpu_in_use || 0 })
      setNodeLabels(n.labels || [])
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
    const x = setInterval(() => {
      fetchLog()
      api.get(`/api/inference/deployments/${detail.id}`).then((r) => setDetail(r.data)).catch(() => {})
    }, 4000)
    return () => clearInterval(x)
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
      kv_connector: 'nixl', node_count: 1, nccl_env_rows: [],
      pipeline_parallel: 1, tp_auto: false, node_target: 'auto', node_labels: [],
      gpu_select: 'auto', image_select: 'auto',
    })
    try {
      const m = await api.get('/api/models', { params: { project_id: projectId } })
      const all = m.data || []
      setModels(all.filter((x: any) => x.status === 'ready' && (x.type === 'base' || x.type === 'merged')))
      setLoraModels(all.filter((x: any) => x.type === 'lora'))
    } catch { /* ignore */ }
    setCreateTab('quick')
    setFormTick(1)  // 进入部署页即按默认值生成启动配置预览
    setView('deploy')
  }

  // NCCL/UCX/NIXL 环境变量覆盖：KEY\VALUE 行 → 对象（提交/预览共用口径）
  const envRowsToObj = (rows: any[]): Record<string, string> => {
    const o: Record<string, string> = {}
    ;(rows || []).forEach((r: any) => {
      const k = r?.key ? String(r.key).trim() : ''
      if (k) o[k] = String(r?.value ?? '')
    })
    return o
  }

  const submitCreate = async () => {
    let v: any
    try { v = await form.validateFields() } catch { return }
    setSubmitting(true)
    try {
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
      // NCCL/UCX/NIXL 环境变量覆盖（KEY\VALUE 行；空=引擎默认）
      const ncclEnv = envRowsToObj(v.nccl_env_rows)
      const r = await api.post('/api/inference/deployments', {
        project_id: projectId, name: v.name, model_asset_id: v.model_asset_id,
        engine: v.engine, quantization: v.quantization,
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
        // 部署放置 / 运行环境（TP 自动 / PP / 目标节点 / 标签约束 / GPU 选择 / 镜像 / Docker 参数 / 命令编辑）
        ...buildPlacement(v),
      })
      message.success(t('infer.created'))
      setView('list')
      load()
      // 创建成功后自动展开新部署行（进度/实例即时可见）
      const newId = r.data?.id
      if (newId) setExpandedKeys((k) => (k.includes(newId) ? k : [...k, newId]))
    } catch (e: any) {
      message.error(e.response?.data?.detail || e.message)
    } finally {
      setSubmitting(false)
    }
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
      engine_version: d.engine_version || '',
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
      engine_version: v.engine_version || '',
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
  const rebuildInst = (depId: number, it: any) => {
    const roleMap: Record<string, string> = { prefill: 'p', decode: 'd', router: 'rt', replica: 'r' }
    api.post(`/api/inference/deployments/${depId}/instances/rebuild`,
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

  // 行操作（⋯ 菜单）：详情 / 编辑 / 启动 / 停止 / 删除
  const onRowAction = (key: string, r: any) => {
    if (key === 'detail') { openDetail(r.id); return }
    if (key === 'edit') { openEdit(r); return }
    if (key === 'start') {
      api.post(`/api/inference/deployments/${r.id}/start`)
        .then(() => {
          message.info(t('infer.startInfo'))
          setExpandedKeys((k) => (k.includes(r.id) ? k : [...k, r.id]))
          load()
        })
        .catch((e) => message.error(e.response?.data?.detail || e.message))
      return
    }
    if (key === 'stop') {
      Modal.confirm({
        title: t('infer.stopConfirm'),
        okButtonProps: { danger: true },
        onOk: () => api.post(`/api/inference/deployments/${r.id}/stop`)
          .then(() => {
            message.success(t('infer.stopped'))
            setExpandedKeys((k) => k.filter((x) => x !== r.id))
            load()
          })
          .catch((e) => message.error(e.response?.data?.detail || e.message)),
      })
      return
    }
    if (key === 'delete') {
      Modal.confirm({
        title: t('infer.deleteConfirm'),
        okButtonProps: { danger: true },
        onOk: () => api.delete(`/api/inference/deployments/${r.id}`)
          .then(() => load())
          .catch((e) => message.error(e.response?.data?.detail || e.message)),
      })
    }
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

  // 实例子表列（行展开 / 详情「实例」共用；重建操作按部署 id 注入）
  const instColumns = (depId: number) => [
    { title: t('infer.instRole'), dataIndex: 'role', width: 100, render: (v: string) => (
      <Tag color={v === 'prefill' ? 'orange' : v === 'decode' ? 'green' : v === 'router' ? 'purple' : 'blue'}>{v}</Tag>
    ) },
    { title: t('infer.instIndex'), dataIndex: 'index', width: 70 },
    { title: t('common.name'), dataIndex: 'name', ellipsis: true },
    { title: t('infer.instAlive'), dataIndex: 'alive', width: 100, render: (v: boolean) => (
      <StatusDot status={v ? t('infer.alive') : t('infer.dead')} tone={v ? 'success' : 'error'} />
    ) },
    { title: t('infer.instIp'), dataIndex: 'ip', width: 140, render: (v: string) => v
      ? <code style={{ fontSize: 11 }}>{v}</code>
      : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span> },
    {
      title: t('common.actions'), width: 90, render: (_: any, it: any) => (
        <Popconfirm title={t('infer.rebuildConfirm', { name: it.name })} okButtonProps={{ danger: true }}
          onConfirm={() => rebuildInst(depId, it)}>
          <Button size="small" icon={<ReloadOutlined />}>{t('infer.rebuild')}</Button>
        </Popconfirm>
      ),
    },
  ]

  // 行展开渲染：实例子表（懒加载 + 展开期间轮询）
  const expandedRowRender = (r: any) => {
    const data = rowInstances[r.id]
    return (
      <div style={{ padding: '6px 12px', background: 'var(--ant-color-fill-quaternary)' }}>
        <Space wrap size={8} style={{ marginBottom: 8 }}>
          {data?.state_message && (
            <span style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12 }}>
              <ExclamationCircleOutlined style={{ marginRight: 6 }} />{data.state_message}
            </span>
          )}
          {data && data.error_count > 0 && <Tag color="red">{t('infer.errCount', { n: data.error_count })}</Tag>}
          {data?.next_restart_at && <Tag color="orange">{t('infer.nextRestart', { at: data.next_restart_at })}</Tag>}
          {/* 展开态快捷切换自动重启（与详情实例页同语义） */}
          <Switch size="small" checked={data ? data.restart_on_error !== false : true}
            onChange={(next: boolean) => {
              api.put(`/api/inference/deployments/${r.id}`, { restart_on_error: next })
                .then(() => setRowInstances((m) => ({ ...m, [r.id]: { ...m[r.id], restart_on_error: next } })))
                .catch((e) => message.error(e.response?.data?.detail || e.message))
            }}
            checkedChildren={t('infer.restartOn')} unCheckedChildren={t('infer.restartOff')} />
        </Space>
        <Table size="small" rowKey={(x: any) => `${x.role}-${x.index}`} pagination={false}
          dataSource={data?.instances || []} columns={instColumns(r.id)} />
      </div>
    )
  }

  const columns: any[] = [
    {
      title: t('common.name'), dataIndex: 'name', width: 210, fixed: 'left',
      render: (v: string, r: any) => (
        <div style={{ minWidth: 0 }}>
          <div style={{ color: 'var(--ant-color-primary)', fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{v}</div>
          <div style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)' }}>
            {r.model}{r.quantization && r.quantization !== 'none' ? ` · ${r.quantization}` : ''}
          </div>
        </div>
      ),
    },
    {
      title: t('infer.colEngine'), dataIndex: 'engine', width: 160,
      render: (v: string, r: any) => (
        <div>
          <Space size={4}>
            <Tag color={v === 'vllm' ? 'blue' : 'cyan'}>{v}</Tag>
            {r.pd_mode === 'pd' && <Tag color="volcano">PD</Tag>}
            {r.cache_service_id && <Tooltip title={t('infer.cacheOn')}><ThunderboltOutlined style={{ color: '#13c2c2' }} /></Tooltip>}
          </Space>
          {(r.backend_name || r.backend_version) && (
            <div style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)' }}>
              {[r.backend_name, r.backend_version].filter(Boolean).join(' ')}
            </div>
          )}
        </div>
      ),
    },
    {
      title: t('infer.replicas'), width: 230,
      render: (_: any, r: any) => (
        <Space size={6} wrap>
          <StatusDot status={stLabel(r.status)} tone={STATUS_TONE[r.status] || 'default'} tip={r.state_message} />
          <span style={{ color: 'var(--ant-color-text-secondary)' }}>
            {r.pd_mode === 'pd'
              ? t('infer.replicasPd', { p: r.prefill_replicas, pt: r.prefill_tp, d: r.decode_replicas, dt: r.decode_tp })
              : t('infer.replicasShort', { n: r.replicas, tp: r.tensor_parallel })}
          </span>
        </Space>
      ),
    },
    {
      title: t('infer.endpoint'), width: 190, render: (_: any, r: any) => r.status === 'running'
        ? <Space size={4}><code style={{ fontSize: 12 }}>/v1/{r.name}</code>
          <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copy(`${location.origin}/v1/${r.name}`)} /></Space>
        : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>,
    },
    {
      title: t('infer.colNode'), width: 120,
      render: () => (nodeName
        ? <span style={{ color: 'var(--ant-color-text-secondary)' }}>{nodeName}</span>
        : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>),
    },
    {
      title: t('common.createTime'), dataIndex: 'created_at', width: 150,
      render: (v: string) => v
        ? <span style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)' }}>{dayjs(v).format('YYYY-MM-DD HH:mm:ss')}</span>
        : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>-</span>,
    },
    {
      title: t('common.actions'), key: 'ops', width: 52, fixed: 'right', align: 'center',
      render: (_: any, r: any) => (
        <Dropdown menu={{
          items: [
            { key: 'detail', icon: <ProfileOutlined />, label: t('common.detail') },
            { key: 'edit', icon: <EditOutlined />, label: t('common.edit') },
            ...(r.status !== 'running' && r.status !== 'creating'
              ? [{ key: 'start', icon: <PlayCircleOutlined />, label: t('infer.start') }]
              : []),
            ...(r.status === 'running'
              ? [{ key: 'stop', icon: <PoweroffOutlined />, label: t('infer.stop'), danger: true }]
              : []),
            { type: 'divider' as const },
            { key: 'delete', icon: <DeleteOutlined />, label: t('common.delete'), danger: true },
          ],
          onClick: ({ key }: any) => onRowAction(key, r),
        }}>
          <Button size="small" type="text" icon={<MoreOutlined />} />
        </Dropdown>
      ),
    },
  ]

  const filtered = list.filter((r) => {
    const k = kw.trim().toLowerCase()
    const okKw = !k || (r.name || '').toLowerCase().includes(k) || (r.model || '').toLowerCase().includes(k)
    const okSt = !stFilter || r.status === stFilter
    return okKw && okSt
  })

  const selModel = Form.useWatch('model_asset_id', form)
  // 「全部参数」计数：已设置的非默认引擎参数 + 环境变量 + 自定义后端
  const paramCount = useMemo(() => {
    const v = form.getFieldsValue(true) || {}
    let n = 0
    if (v.engine === 'vllm') {
      if (v.adv_max_num_seqs != null && v.adv_max_num_seqs !== '') n++
      if (v.adv_dtype) n++
      if (v.adv_enforce_eager) n++
      if (v.adv_prefix_caching) n++
      if (v.adv_chunked_prefill) n++
      if (v.adv_api_key?.trim()) n++
      if (v.adv_cpu_offload_gb != null && v.adv_cpu_offload_gb !== '') n++
    } else {
      if (v.adv_mem_fraction_static != null && v.adv_mem_fraction_static !== '') n++
      if (v.adv_disable_cuda_graph) n++
      if (v.adv_hicache_size != null && v.adv_hicache_size !== '') n++
    }
    if (v.adv_extra_args?.trim()) n++
    if ((v.nccl_env_rows || []).some((r: any) => r && r.key && String(r.key).trim())) n++
    if (v.backend_name) n++
    return n
  }, [formTick])

  // 部署页（GPUWorks 式整页布局：页头 + 快捷/专家切换 + 分组卡片 + 配置评估 + 启动配置预览）
  const renderDeploy = () => (
    <div style={{ maxWidth: 1120, margin: '0 auto', paddingBottom: 40 }}>
      {/* 页头：标题+副标题；右=返回/快捷部署/专家部署 */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 16, gap: 12, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 20, fontWeight: 700 }}>{t('infer.deployPageTitle')}</div>
          <div style={{ color: 'var(--ant-color-text-secondary)', fontSize: 13, marginTop: 4 }}>{t('infer.deployPageSub')}</div>
        </div>
        <Space wrap>
          <Button icon={<ArrowLeftOutlined />} onClick={() => setView('list')}>{t('infer.backToList')}</Button>
          <Button type={createTab === 'quick' ? 'primary' : 'default'} onClick={() => setCreateTab('quick')}>{t('infer.tabQuick')}</Button>
          <Button type={createTab === 'expert' ? 'primary' : 'default'} onClick={() => setCreateTab('expert')}>{t('infer.tabExpert')}</Button>
        </Space>
      </div>

      {createTab === 'quick' ? (
        <>
          <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginBottom: 12 }}>{t('infer.quickHint')}</div>
          <Form form={form} layout="vertical" onValuesChange={() => setFormTick((n) => n + 1)}>
            <Group title={t('infer.cardBasic')}>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="model_asset_id" label={t('infer.modelAsset')} extra={t('infer.modelTip')} rules={[{ required: true }]}>
                    <Select showSearch optionFilterProp="label"
                      options={models.map((m: any) => ({ value: m.id, label: `${m.name} (${m.type})` }))} />
                  </Form.Item>
                </Col>
                <Col span={6}>
                  <Form.Item name="engine" label={t('infer.engine')}>
                    <Select options={[{ value: 'vllm', label: 'vLLM' }, { value: 'sglang', label: 'SGLang' }]}
                      onChange={() => form.setFieldsValue({ engine_version: '' })} />
                  </Form.Item>
                </Col>
                <Col span={6}>
                  <Form.Item name="engine_version" label={t('infer.engineVersion')} extra={t('infer.engineVersionTip')}>
                    {/* AutoComplete：列表可用时选 tag；离线（Docker Hub 不可达）仍可手输 */}
                    <AutoComplete style={{ width: '100%' }} options={evOptions(eng)}
                      placeholder={t('infer.engineVersionFollow', { v: engVersions[eng]?.current || '—' })} />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="name" label={t('infer.deployName')} rules={[{ required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: t('infer.nameRule') }]}>
                    <Input placeholder={t('infer.namePh')} maxLength={60} showCount />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="description" label={t('infer.desc')}>
                    <Input placeholder={t('infer.descPh')} maxLength={200} showCount />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={16}>
                <Col span={24}>
                  <Form.Item name="lora_assets" label={t('infer.extraLora')} extra={t('infer.loraTip')}>
                    <Select mode="multiple"
                      options={loraModels.map((m: any) => ({ value: m.id, label: m.name }))}
                      placeholder={t('common.optional')} />
                  </Form.Item>
                </Col>
              </Row>
            </Group>
            <Group title={t('infer.cardResource')}>
              <Row gutter={16}>
                <Col span={6}><Form.Item name="tensor_parallel" label={t('infer.tp')} extra={t('infer.tpTip')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                <Col span={6}><Form.Item name="replicas" label={t('infer.replicas')} extra={t('infer.replicasTip')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                <Col span={6}><Form.Item name="max_model_len" label={t('infer.maxLen')} extra={t('infer.maxLenTip')}><InputNumber min={1024} step={1024} style={{ width: '100%' }} /></Form.Item></Col>
                <Col span={6}><Form.Item name="gpu_mem_util" label={t('infer.memUtil')} extra={t('infer.memUtilTip')}><InputNumber step={0.05} min={0.3} max={0.95} style={{ width: '100%' }} /></Form.Item></Col>
              </Row>
              <div style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12, marginTop: -12 }}>
                <ThunderboltOutlined />{' '}
                {t('infer.gpuNeed', { n: gpuNeed, tp, r: rep })}
              </div>
            </Group>
            <Group title={t('infer.cardPreview')}>
              <LaunchPreview form={form} tick={formTick} loraModels={loraModels} t={t} onPreview={setPreviewRes} />
            </Group>
          </Form>
        </>
      ) : (
        <Form form={form} layout="vertical" onValuesChange={() => setFormTick((n) => n + 1)}>
          <Group title={t('infer.cardBasic')}>
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="model_asset_id" label={t('infer.modelAsset')} extra={t('infer.modelTip')} rules={[{ required: true }]}>
                  <Select showSearch optionFilterProp="label"
                    options={models.map((m: any) => ({ value: m.id, label: `${m.name} (${m.type})` }))} />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item name="engine" label={t('infer.engine')}>
                  <Select options={[{ value: 'vllm', label: 'vLLM' }, { value: 'sglang', label: 'SGLang' }]}
                    onChange={() => form.setFieldsValue({ engine_version: '' })} />
                </Form.Item>
              </Col>
              <Col span={6}>
                <Form.Item name="engine_version" label={t('infer.engineVersion')} extra={t('infer.engineVersionTip')}>
                  <AutoComplete style={{ width: '100%' }} options={evOptions(eng)}
                    placeholder={t('infer.engineVersionFollow', { v: engVersions[eng]?.current || '—' })} />
                </Form.Item>
              </Col>
            </Row>
            <Row gutter={16}>
              <Col span={12}>
                <Form.Item name="name" label={t('infer.deployName')} rules={[{ required: true, pattern: /^[a-zA-Z0-9_-]+$/, message: t('infer.nameRule') }]}>
                  <Input placeholder={t('infer.namePh')} maxLength={60} showCount />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item name="adv_served_model_name" label={t('infer.servedName')} extra={t('infer.servedNameTip')}>
                  <Input placeholder={t('infer.advDefault')} maxLength={120} />
                </Form.Item>
              </Col>
            </Row>
            <Form.Item name="description" label={t('infer.desc')}>
              <Input.TextArea placeholder={t('infer.descPh')} maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
            </Form.Item>
          </Group>

          {/* 全部参数：按引擎参数字典分组（模型与上下文 / 性能 / 资源配置 / 运行环境） */}
          <Card size="small"
            title={<span style={{ fontSize: 13, fontWeight: 600 }}>{t('infer.allParams')}</span>}
            extra={<Tag color="blue">{t('infer.paramsCount', { n: paramCount })}</Tag>}
            style={{ marginBottom: 12 }}>
            <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginBottom: 4 }}>{t('infer.allParamsHint')}</div>
            <Tabs size="small" items={[
              {
                key: 'ctx', label: t('infer.subModelCtx'), children: (
                  <>
                    <Row gutter={16}>
                      <Col span={8}><Form.Item name="quantization" label={t('infer.quant')} extra={t('infer.quantTip')}><Select options={['none', 'awq', 'gptq', 'int8'].map((q) => ({ value: q, label: q }))} /></Form.Item></Col>
                      <Col span={8}><Form.Item name="max_model_len" label={t('infer.maxLen')} extra={t('infer.maxLenTip')}><InputNumber min={1024} step={1024} style={{ width: '100%' }} /></Form.Item></Col>
                      <Col span={8}><Form.Item name="gpu_mem_util" label={t('infer.memUtil')} extra={t('infer.memUtilTip')}><InputNumber step={0.05} min={0.3} max={0.95} style={{ width: '100%' }} /></Form.Item></Col>
                    </Row>
                    <Row gutter={16}>
                      <Col span={12}>
                        {eng === 'sglang' ? (
                          <Form.Item name="adv_hicache_size" label={t('infer.hicacheSize')} tooltip={t('infer.hicacheSizeHint')}>
                            <InputNumber min={1} max={512} step={1} placeholder={t('infer.advDefault')} addonAfter="GB" style={{ width: '100%' }} />
                          </Form.Item>
                        ) : (
                          <Form.Item name="adv_cpu_offload_gb" label={t('infer.cpuOffloadGb')} tooltip={t('infer.cpuOffloadGbHint')}>
                            <InputNumber min={1} max={256} step={1} placeholder={t('infer.advDefault')} addonAfter="GB" style={{ width: '100%' }} />
                          </Form.Item>
                        )}
                      </Col>
                      <Col span={12}>
                        <Form.Item name="lora_assets" label={t('infer.extraLora')} extra={t('infer.loraTip')}>
                          <Select mode="multiple" options={loraModels.map((m: any) => ({ value: m.id, label: m.name }))} placeholder={t('common.optional')} />
                        </Form.Item>
                      </Col>
                    </Row>
                  </>
                ),
              },
              {
                key: 'perf', label: t('infer.subPerf'), children: (
                  <>
                    <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginBottom: 8 }}>{t('infer.advHint')}</div>
                    {eng === 'vllm' ? (
                      <>
                        <Row gutter={16}>
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
                            <Form.Item name="adv_api_key" label={t('infer.advApiKey')}>
                              <Input.Password placeholder={t('infer.advDefault')} autoComplete="off" maxLength={128} />
                            </Form.Item>
                          </Col>
                        </Row>
                        <Row gutter={16}>
                          <Col span={8}><Form.Item name="adv_enforce_eager" label={t('infer.advEnforceEager')} valuePropName="checked"><Switch /></Form.Item></Col>
                          <Col span={8}><Form.Item name="adv_prefix_caching" label={t('infer.advPrefixCaching')} valuePropName="checked"><Switch /></Form.Item></Col>
                          <Col span={8}><Form.Item name="adv_chunked_prefill" label={t('infer.advChunkedPrefill')} valuePropName="checked"><Switch /></Form.Item></Col>
                        </Row>
                      </>
                    ) : (
                      <Row gutter={16}>
                        <Col span={12}>
                          <Form.Item name="adv_mem_fraction_static" label={t('infer.advMemFraction')}>
                            <InputNumber step={0.05} min={0.3} max={0.95} placeholder={t('infer.advDefault')} style={{ width: '100%' }} />
                          </Form.Item>
                        </Col>
                        <Col span={12}><Form.Item name="adv_disable_cuda_graph" label={t('infer.advDisableCudaGraph')} valuePropName="checked"><Switch /></Form.Item></Col>
                      </Row>
                    )}
                    <Form.Item name="adv_extra_args" label={t('infer.advExtraArgs')} tooltip={t('infer.advExtraArgsHint')} style={{ marginTop: 8 }}>
                      <Input.TextArea placeholder={t('infer.advExtraArgsPh')} autoSize={{ minRows: 1, maxRows: 3 }} />
                    </Form.Item>
                  </>
                ),
              },
              {
                key: 'res', label: t('infer.cardResource'), children: (
                  <>
                    <Row gutter={16}>
                      <Col span={6}>
                        <Form.Item name="tp_auto" label={t('infer.tpAuto')} extra={t('infer.tpAutoTip')} valuePropName="checked">
                          <Switch checkedChildren="ON" unCheckedChildren="OFF" />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="tensor_parallel" label={t('infer.tp')}
                          extra={tpAuto ? t('infer.tpAutoOn') : t('infer.tpTip')}>
                          <InputNumber min={1} max={8} disabled={tpAuto || pdMode === 'pd'} style={{ width: '100%' }} />
                        </Form.Item>
                      </Col>
                      <Col span={6}><Form.Item name="replicas" label={t('infer.replicas')} extra={t('infer.replicasTip')}><InputNumber min={1} max={8} disabled={pdMode === 'pd'} style={{ width: '100%' }} /></Form.Item></Col>
                      <Col span={6}>
                        <Form.Item name="pd_mode" label={t('infer.pdMode')}
                          tooltip={selBackendObj && selBackendObj.source === 'custom'
                            ? t('infer.pdDisabledCustom') : t('infer.pdModeHint')}>
                          <Select disabled={!!selBackendObj && selBackendObj.source === 'custom'} options={[
                            { value: 'off', label: t('infer.pdOff') },
                            { value: 'pd', label: t('infer.pdOn') },
                          ]} />
                        </Form.Item>
                      </Col>
                    </Row>
                    {tpAuto && pdMode !== 'pd' && previewRes?.tp_auto && (
                      <div style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12, marginBottom: 12 }}>
                        <ThunderboltOutlined /> {t('infer.tpAutoResult')}：TP = <b>{previewRes.tp_auto.value}</b>
                        <span style={{ color: 'var(--ant-color-text-tertiary)' }}>（{previewRes.tp_auto.reason}）</span>
                      </div>
                    )}
                    {pdMode === 'pd' && (
                      <Row gutter={16}>
                        <Col span={6}><Form.Item name="prefill_replicas" label={t('infer.prefillReplicas')} tooltip={t('infer.prefillReplicasHint')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                        <Col span={6}><Form.Item name="prefill_tp" label={t('infer.prefillTp')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                        <Col span={6}><Form.Item name="decode_replicas" label={t('infer.decodeReplicas')} tooltip={t('infer.decodeReplicasHint')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                        <Col span={6}><Form.Item name="decode_tp" label={t('infer.decodeTp')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                        <Col span={6}><Form.Item name="kv_connector" label={t('infer.kvConnector')} tooltip={t('infer.kvConnectorHint')}><Select options={[
                          { value: 'nixl', label: 'NIXL' }, { value: 'mooncake', label: 'Mooncake' },
                        ]} /></Form.Item></Col>
                      </Row>
                    )}
                    <Row gutter={16}>
                      <Col span={6}><Form.Item name="pipeline_parallel" label={t('infer.pp')} extra={t('infer.ppTip')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                      <Col span={6}>
                        <Form.Item name="node_target" label={t('infer.targetNode')} extra={t('infer.targetNodeTip')}>
                          <Select options={[
                            { value: 'auto', label: t('infer.targetNodeAuto') },
                            { value: 'manual', label: t('infer.targetNodeManual') },
                          ]} />
                        </Form.Item>
                      </Col>
                      {nodeTarget === 'manual' && (
                        <Col span={6}>
                          <Form.Item name="node_name" label={t('infer.nodePick')} rules={[{ required: true }]}>
                            <Select options={[{ value: nodeName, label: `${nodeName} (${t('cluster.nodeLocal')})` }]} />
                          </Form.Item>
                        </Col>
                      )}
                      <Col span={6}>
                        <Form.Item label={t('infer.autoSelectResult')}>
                          {previewRes?.node ? (
                            <Space size={4} wrap>
                              <Tag color="blue">{previewRes.node.name}</Tag>
                              <span style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)' }}>
                                {t('infer.nodeTotalFree', { total: previewRes.node.total, free: previewRes.node.free })}
                              </span>
                            </Space>
                          ) : <span style={{ color: 'var(--ant-color-text-quaternary)' }}>—</span>}
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={16}>
                      <Col span={6}>
                        <Form.Item name="node_labels" label={t('infer.nodeLabels')} tooltip={t('infer.nodeLabelsTip')}>
                          <Select mode="tags" open={false} placeholder={t('infer.nodeLabelsPh')}
                            tokenSeparators={[',', '\n']} style={{ minWidth: 160 }} />
                        </Form.Item>
                      </Col>
                      <Col span={6}>
                        <Form.Item name="gpu_select" label={t('infer.gpuSelect')}
                          extra={pdMode === 'pd' ? t('infer.gpuPdDisabled') : t('infer.gpuSelectTip')}>
                          <Select disabled={pdMode === 'pd'} options={[
                            { value: 'auto', label: t('infer.gpuSelectAuto') },
                            { value: 'manual', label: t('infer.gpuSelectManual') },
                          ]} />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item name="gpu_ids_str" label={t('infer.gpuIds')} extra={t('infer.gpuIdsTip')}>
                          <Input placeholder={t('infer.gpuIdsPh')}
                            disabled={pdMode === 'pd' || gpuSelect !== 'manual'}
                            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 12 }} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <Row gutter={16}>
                      <Col span={6}><Form.Item name="node_count" label={t('infer.nodeCount')} extra={t('infer.nodeCountHint')}><InputNumber min={1} max={8} style={{ width: '100%' }} /></Form.Item></Col>
                      <Col span={6}>
                        <Form.Item label={t('infer.formNode')} extra={t('infer.formNodeHint')}>
                          <Input disabled value={nodeName || '—'} />
                        </Form.Item>
                      </Col>
                      <Col span={12}>
                        <Form.Item label={t('infer.rdmaStatus')} tooltip={t('infer.rdmaStatusHint')} style={{ marginBottom: 24 }}>
                          {rdma.present
                            ? <Tag color="green">{t('infer.rdmaOn', { d: rdma.devices.join(', ') })}</Tag>
                            : <Tag>{t('infer.rdmaOff')}</Tag>}
                        </Form.Item>
                      </Col>
                    </Row>
                    <div style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12 }}>
                      <ThunderboltOutlined />{' '}
                      {pdMode === 'pd'
                        ? t('infer.gpuNeedPd', { p: prefillReplicas, pt: prefillTp, d: decodeReplicas, dt: decodeTp, n: gpuNeed })
                        : t('infer.gpuNeed', { n: tpAuto ? t('infer.auto') : gpuNeed, tp: tpAuto ? t('infer.auto') : tp, r: rep })}
                    </div>
                  </>
                ),
              },
              {
                key: 'rt', label: t('infer.cardEnv'), children: (
                  <>
                    <Row gutter={16}>
                      <Col span={8}>
                        <Form.Item name="image_select" label={t('infer.imageSelect')}>
                          <Select options={[
                            { value: 'auto', label: t('infer.imageSelectAuto') },
                            { value: 'custom', label: t('infer.imageSelectCustom') },
                          ]} />
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        {imageSelect === 'custom' ? (
                          <Form.Item name="image_ref" label={t('infer.imageRef')} extra={t('infer.imageRefTip')}
                            rules={[{ required: true, message: t('infer.imageRefReq') }]}>
                            <Input placeholder={t('infer.imageRefPh')} maxLength={255}
                              style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 12 }} />
                          </Form.Item>
                        ) : (
                          <Form.Item label={t('infer.dImage')} extra={t('infer.imageAutoTip')}>
                            <code style={{ fontSize: 12, background: 'var(--ant-color-fill-tertiary)', padding: '4px 8px', borderRadius: 4 }}>
                              {(selBackendObj && selBackendObj.source === 'custom'
                                ? (selBackendObj.versions.find((v: any) => v.version === selBackendVer)?.image
                                  || selBackendObj.current_image)
                                : ENGINE_IMAGES[eng]) || '—'}
                            </code>
                          </Form.Item>
                        )}
                      </Col>
                      <Col span={8}>
                        <Form.Item name="backend_name" label={t('infer.backend')} tooltip={t('infer.backendHint')}>
                          <Select allowClear showSearch optionFilterProp="label"
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
                      </Col>
                    </Row>
                    <Row gutter={16}>
                      <Col span={8}>
                        {selBackendObj && selBackendObj.source === 'custom' && (
                          <Form.Item name="backend_version" label={t('infer.backendVer')} tooltip={t('infer.backendVerHint')}>
                            <Select allowClear placeholder={t('infer.backendVerDefault')}
                              options={selBackendObj.versions.map((v: any) => ({
                                value: v.version,
                                label: v.is_default ? `${v.version} (${t('infer.defaultVer')})` : v.version,
                              }))} />
                          </Form.Item>
                        )}
                      </Col>
                      <Col span={8}>
                        <Form.Item name="cache_service_id" label={t('infer.cacheSvc')} tooltip={t('infer.cacheSvcHint')}>
                          <Select allowClear showSearch optionFilterProp="label"
                            placeholder={t('infer.cacheSvcNone')}
                            options={cacheServices.map((c: any) => ({ value: c.id, label: c.name }))} />
                        </Form.Item>
                      </Col>
                      <Col span={8}>
                        <Form.Item name="docker_args" label={t('infer.dockerArgs')} tooltip={t('infer.dockerArgsTip')}>
                          <Input.TextArea placeholder={t('infer.dockerArgsPh')} autoSize={{ minRows: 2, maxRows: 4 }}
                            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 12 }} />
                        </Form.Item>
                      </Col>
                    </Row>
                    <div style={{ fontWeight: 600, fontSize: 13, marginTop: 4, marginBottom: 8 }}>{t('infer.ncclEnv')}</div>
                    <Form.List name="nccl_env_rows">
                      {(fields, { add, remove }) => (
                        <div>
                          {fields.map(({ key, name, ...rest }) => (
                            <Row gutter={8} key={key} align="middle">
                              <Col span={10}>
                                <Form.Item {...rest} name={[name, 'key']} style={{ marginBottom: 8 }}>
                                  <Input placeholder="NCCL_P2P_DISABLE" />
                                </Form.Item>
                              </Col>
                              <Col span={12}>
                                <Form.Item {...rest} name={[name, 'value']} style={{ marginBottom: 8 }}>
                                  <Input placeholder="1" />
                                </Form.Item>
                              </Col>
                              <Col span={2}>
                                <Button type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(name)} />
                              </Col>
                            </Row>
                          ))}
                          <Button type="dashed" icon={<PlusOutlined />} onClick={() => add()}>
                            {t('infer.addEnvVar')}
                          </Button>
                        </div>
                      )}
                    </Form.List>
                    <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginTop: 4 }}>{t('infer.envVarTip')}</div>
                    {/* 本次部署的启动设置：编辑命令文本（覆盖自动拼装；仅协同模式） */}
                    <Collapse ghost size="small" style={{ marginTop: 12, background: 'var(--ant-color-fill-quaternary)', borderRadius: 6 }}
                      items={[{
                        key: 'launch',
                        label: <span style={{ fontWeight: 600, fontSize: 13 }}>{t('infer.launchSettings')}</span>,
                        children: pdMode === 'pd' ? (
                          <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12 }}>{t('infer.cmdPdDisabled')}</div>
                        ) : (
                          <>
                            <Form.Item name="cmd_override" label={t('infer.editCmd')} tooltip={t('infer.cmdOverrideTip')} style={{ marginBottom: 4 }}>
                              <Input.TextArea autoSize={{ minRows: 3, maxRows: 8 }} placeholder={t('infer.cmdOverridePh')}
                                style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 12 }} />
                            </Form.Item>
                            <Button size="small" type="link" style={{ paddingLeft: 0 }}
                              onClick={() => form.setFieldsValue({ cmd_override: '' })}>
                              {t('infer.restoreCmd')}
                            </Button>
                          </>
                        ),
                      }]} />
                  </>
                ),
              },
            ]} />
          </Card>

          {/* 配置评估（蓝左边框）：GPU 需求 / 节点 / 可用性 / RDMA */}
          <Card size="small" style={{ marginBottom: 12, borderLeft: '3px solid var(--ant-color-primary)' }}
            title={<span style={{ fontSize: 13, fontWeight: 600 }}>{t('infer.evalCard')}</span>}>
            {selModel ? (
              <Space size={20} wrap>
                <span style={{ fontSize: 13 }}>
                  {t('infer.evalGpu')}：<b>{tpAuto && pdMode !== 'pd' ? t('infer.auto') : gpuNeed}</b>
                </span>
                <span style={{ fontSize: 13, color: 'var(--ant-color-text-secondary)' }}>
                  {t('infer.evalTargetNode')}：{nodeTarget === 'manual' ? (nodeName || '—') : t('infer.targetNodeAuto')}
                </span>
                {nodeLabelsSel.length > 0 && (
                  missingNodeLabels.length > 0
                    ? <Tag color="red">{t('infer.nodeLabelsMiss', { l: missingNodeLabels.join(', ') })}</Tag>
                    : <Tag color="green">{t('infer.nodeLabelsMatch')}</Tag>
                )}
                {nodeGpu.total > 0 && (
                  <span style={{ fontSize: 13, color: 'var(--ant-color-text-secondary)' }}>
                    {t('infer.evalGpuAvail', { avail: Math.max(0, nodeGpu.total - nodeGpu.used), total: nodeGpu.total })}
                  </span>
                )}
                <span style={{ fontSize: 13, color: 'var(--ant-color-text-secondary)' }}>
                  {t('infer.evalRdma')}：{rdma.present
                    ? <Tag color="green">{t('infer.rdmaOn', { d: rdma.devices.join(', ') })}</Tag>
                    : <Tag>{t('infer.rdmaOff')}</Tag>}
                </span>
              </Space>
            ) : (
              <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12 }}>{t('infer.evalEmpty')}</div>
            )}
          </Card>

          <Group title={t('infer.cardPreview')}>
            <LaunchPreview form={form} tick={formTick} loraModels={loraModels} t={t} onPreview={setPreviewRes} />
          </Group>
        </Form>
      )}

      {/* 部署按钮（右下，GPUWorks 同位） */}
      <div style={{ textAlign: 'right', marginTop: 20 }}>
        <Button type="primary" size="large" loading={submitting} onClick={submitCreate}>{t('infer.deployBtn')}</Button>
      </div>
    </div>
  )

  if (view === 'deploy') return renderDeploy()
  return (
    <div>
      {/* 工具栏：左=搜索+状态过滤+刷新；右=批量删除(N)+新建（列表页无大标题） */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
        <Space wrap>
          <Input allowClear prefix={<SearchOutlined style={{ color: 'var(--ant-color-text-quaternary)' }} />}
            placeholder={t('infer.searchPh')} value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 260 }} />
          <Select value={stFilter} onChange={setStFilter} style={{ width: 140 }}
            options={[
              { value: '', label: t('infer.all') },
              { value: 'running', label: t('infer.stRunning') },
              { value: 'creating', label: t('infer.stCreating') },
              { value: 'stopped', label: t('infer.stStopped') },
              { value: 'failed', label: t('infer.stFailed') },
            ]} />
          <Button type="text" icon={<ReloadOutlined />} onClick={load} />
        </Space>
        <Space>
          {sel.length > 0 && <Button danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button type="primary" icon={<PlusOutlined />} onClick={openWizard}>{t('infer.newDeploy')}</Button>
        </Space>
      </div>
      <Table rowKey="id" size="small" dataSource={filtered} pagination={{ pageSize: 12, showSizeChanger: false }}
        scroll={{ x: 1240 }}
        rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k.map(Number)) }}
        expandable={{
          expandedRowKeys: expandedKeys,
          onExpandedRowsChange: (keys: any) => setExpandedKeys(keys.map(Number)),
          expandedRowRender,
        }}
        locale={{ emptyText: t('infer.empty') }}
        columns={columns} />


      {/* 编辑部署（右侧 Drawer）：即时字段（名/描述/自动重启/后端）直改；参数字段触发实例重建（后端判定） */}
      <Drawer title={editTarget ? `${t('infer.editTitle')} · ${editTarget.name}` : t('infer.editTitle')}
        width={640} open={!!editTarget} onClose={() => setEditTarget(null)}
        footer={<div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => setEditTarget(null)}>{t('common.cancel')}</Button>
            <Button type="primary" onClick={async () => {
              const v = await editForm.validateFields()
              // PD 部署不走协同 TP/副本参数（编辑面板未暴露 P/D 角色参数，禁改防误操作）
              if (editTarget.pd_mode === 'pd' &&
                  (v.tensor_parallel !== editTarget.tensor_parallel || v.replicas !== editTarget.replicas)) {
                message.error(t('infer.pdEditNotAllowed')); return
              }
              await submitEdit()
            }}>{t('common.confirm')}</Button>
          </Space>
        </div>}>
        <Form form={editForm} layout="vertical">
          <SectionTitle>{t('infer.secBasic')}</SectionTitle>
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

          <SectionTitle>{t('infer.secBackend')}</SectionTitle>
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
          <div style={{ color: 'var(--ant-color-text-tertiary)', fontSize: 12, marginTop: -4 }}>{t('infer.editRebuildHint')}</div>

          <SectionTitle>{t('infer.secParams')}</SectionTitle>
          <Space wrap>
            <Form.Item name="engine" label={t('infer.engine')}
              rules={[{ required: true }]}>
              <Select style={{ width: 130 }} options={[{ value: 'vllm', label: 'vLLM' }, { value: 'sglang', label: 'SGLang' }]}
                onChange={() => editForm.setFieldsValue({ engine_version: '' })} />
            </Form.Item>
            <Form.Item name="engine_version" label={t('infer.engineVersion')} tooltip={t('infer.engineVersionTip')}>
              <AutoComplete style={{ width: 220 }} options={evOptions(editEngine)}
                placeholder={t('infer.engineVersionFollow', { v: engVersions[editEngine]?.current || '—' })} />
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
        </Form>
      </Drawer>

      <Drawer title={detail ? t('infer.detailTitleWith', { name: detail.name }) : t('infer.detailTitle')} width={760} open={!!detail} onClose={() => setDetail(null)}>
        {detail && (
          <Tabs items={[
            {
              key: 'invoke', label: <span><CodeOutlined /> {t('infer.tabInvoke')}</span>, children: (
                <div>
                  <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}
                    items={[
                      { key: 1, label: t('common.status'), children: (
                        <StatusDot status={stLabel(detail.status)} tone={STATUS_TONE[detail.status] || 'default'} tip={detail.state_message} />
                      ) },
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
                      { key: 'img', label: t('infer.dImage'), children: <code style={{ fontSize: 12 }}>{detail.image_ref || ENGINE_IMAGES[detail.engine] || detail.engine}</code> },
                      {
                        key: 'placement', label: t('infer.dPlacement'),
                        children: (
                          <Space size={4} wrap>
                            <Tag>{t('infer.evalTargetNode')}：{detail.node_name || t('infer.targetNodeAuto')}</Tag>
                            {(detail.node_labels || []).map((l: string) => <Tag key={l} color="geekblue">{l}</Tag>)}
                            <Tag>{detail.gpu_ids?.length
                              ? t('infer.gpuIdsAssigned', { g: detail.gpu_ids.join(', ') })
                              : t('infer.gpuAutoAlloc')}</Tag>
                            {(detail.pipeline_parallel || 1) > 1 && <Tag>PP {detail.pipeline_parallel}</Tag>}
                            {(detail.docker_args || []).map((a: string) => <Tag key={a}>{a}</Tag>)}
                          </Space>
                        ),
                      },
                      ...(detail.cmd_override
                        ? [{ key: 'cmdOv', label: t('infer.editCmd'), children: (
                          <pre style={{ margin: 0, fontSize: 11, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{detail.cmd_override}</pre>
                        ) }]
                        : []),
                      { key: 'node', label: t('infer.colNode'), children: nodeName || '-' },
                      { key: 4, label: t('infer.colModel'), children: detail.model },
                      { key: 5, label: t('infer.dMaxLen'), children: `${detail.max_model_len} / ${detail.gpu_mem_util}` },
                      {
                        key: 6, label: t('infer.baseUrl'),
                        children: <Space><code>{base}</code>
                          <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copy(base)} /></Space>,
                      },
                    ]} />
                  <p style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12, marginTop: -8 }}>{t('infer.invokeHint')}</p>
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
                    <span style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12 }}>{t('infer.restartHint')}</span>
                    {inst && inst.error_count > 0 && (
                      <Tag color="red">{t('infer.errCount', { n: inst.error_count })}</Tag>
                    )}
                    {inst && inst.next_restart_at && (
                      <Tag color="orange">{t('infer.nextRestart', { at: inst.next_restart_at })}</Tag>
                    )}
                  </Space>
                  {inst?.state_message && (
                    <p style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12, marginTop: 0, marginBottom: 8 }}>
                      <ExclamationCircleOutlined style={{ marginRight: 6 }} />{inst.state_message}
                    </p>
                  )}
                  <Table size="small" rowKey={(r: any) => `${r.role}-${r.index}`} pagination={false}
                    dataSource={inst?.instances || []}
                    columns={instColumns(detail.id)} />
                </div>
              ),
            },
            {
              key: 'play', label: <span><ThunderboltOutlined /> {t('infer.tabPlay')}</span>, children: (
                <div style={{ display: 'flex', flexDirection: 'column', height: 480 }}>
                  <div style={{ flex: 1, overflow: 'auto', background: 'var(--ant-color-fill-quaternary)', borderRadius: 8, padding: 12 }}>
                    {chat.length === 0 && <span style={{ color: 'var(--ant-color-text-tertiary)' }}>{t('infer.playHint')}</span>}
                    {chat.map((m, i) => (
                      <div key={i} style={{ marginBottom: 10 }}>
                        <Tag color={m.role === 'user' ? 'blue' : 'green'}>{m.role}</Tag>
                        <div style={{ whiteSpace: 'pre-wrap', marginTop: 4 }}>{m.content}</div>
                      </div>
                    ))}
                    {chatLoading && <span style={{ color: 'var(--ant-color-text-secondary)' }}>{t('infer.thinking')}</span>}
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
                      {
                        title: t('common.actions'), width: 90, render: (_: any, r: any) => (
                          <Popconfirm title={t('infer.unloadConfirm', { name: r.name })} okButtonProps={{ danger: true }}
                            onConfirm={async () => { await api.delete(`/api/inference/deployments/${detail.id}/adapters/${r.name}`); openDetail(detail.id) }}>
                            <Button size="small" danger>{t('infer.unload')}</Button>
                          </Popconfirm>
                        ),
                      },
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
                  <pre style={{ background: 'var(--ant-color-fill-quaternary)', padding: 12, borderRadius: 6, maxHeight: 460, overflow: 'auto', fontSize: 12 }}>{logs || t('infer.noLogs')}</pre>
                </div>
              ),
            },
            {
              key: 'scale', label: t('infer.tabScale'), children: (
                detail.pd_mode === 'pd' ? (
                  <div>
                    <p style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12, marginTop: 0 }}>{t('infer.pdScaleHint')}</p>
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

/** 引擎监控：KV 缓存 / 运行中·排队请求 / 平均延迟（约 30s 采集一次，保留 24h） */
function MonitorPanel({ mon }: { mon: any }) {
  const { t, i18n: ti18n } = useTranslation()
  const ser = mon?.series || []
  const last = ser[ser.length - 1]
  const data = ser.map((x: any) => ({
    t: new Date(x.ts * 1000).toLocaleTimeString(ti18n.language, { hour: '2-digit', minute: '2-digit' }),
    kv: Math.round(x.kv * 10) / 10, running: x.running, waiting: x.waiting,
  }))
  if (!last) return <span style={{ color: 'var(--ant-color-text-tertiary)' }}>{t('infer.monEmpty')}</span>
  return (
    <div>
      <p style={{ color: 'var(--ant-color-text-secondary)', fontSize: 12, marginTop: 0 }}>{t('infer.monHint')}</p>
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
