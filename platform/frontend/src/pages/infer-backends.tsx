import React, { useEffect, useState, useCallback } from 'react'
import {
  Row, Col, Card, Button, Space, Tag, Form, Input,
  Select, message, Modal, Upload, Tooltip, Switch, Dropdown, Drawer, Empty,
} from 'antd'
import {
  PlusOutlined, DeleteOutlined, ImportOutlined,
  StarOutlined, CodeOutlined, CopyOutlined,
  SearchOutlined, MoreOutlined, ReloadOutlined,
  RocketOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { SectionTitle } from './infer-shared'

// ---------- 卡片图标：builtin 用引擎图标，custom 按 id 确定性取色+数学符号（参照 GPUStack） ----------
const ENGINE_META: Record<string, { icon: React.ReactNode; color: string }> = {
  vllm: { icon: <RocketOutlined />, color: '#16a34a' },
  sglang: { icon: <ThunderboltOutlined />, color: '#ea580c' },
}
const CUSTOM_COLORS = ['#0284c7', '#7c3aed', '#db2777', '#0d9488', '#d97706', '#4f46e5', '#dc2626', '#65a30d']
const CUSTOM_SYMBOLS = ['∑', '∏', '∫', '∂', '∞', 'π', 'θ', '∆']
const FRAME_COLOR: Record<string, string> = { cuda: 'green', rocm: 'volcano', cpu: 'default' }

function BackendIcon({ b }: { b: any }) {
  const meta = ENGINE_META[b.name]
  const box: React.CSSProperties = {
    width: 32, height: 32, borderRadius: 8, flex: 'none',
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: 16,
  }
  if (meta) {
    return <span style={{ ...box, background: `${meta.color}1a`, color: meta.color }}>{meta.icon}</span>
  }
  const color = CUSTOM_COLORS[b.id % CUSTOM_COLORS.length]
  return (
    <span style={{ ...box, background: `${color}1a`, color, fontWeight: 600 }}>
      {CUSTOM_SYMBOLS[b.id % CUSTOM_SYMBOLS.length]}
    </span>
  )
}

/** 后端全部版本的框架聚合：{fw, vers[]}（框架标签 tooltip 展示对应版本） */
function frameworksOf(b: any): { fw: string; vers: string[] }[] {
  const m = new Map<string, string[]>()
  for (const v of b.versions || []) {
    const fw = v.framework || 'cuda'
    m.set(fw, [...(m.get(fw) || []), v.version])
  }
  return [...m.entries()].map(([fw, vers]) => ({ fw, vers }))
}

/** 推理后端注册表：builtin（vllm/sglang，镜像走 pin 系统）/ custom（版本决定镜像+启动命令模板）
 *  卡片网格布局（参照 GPUStack backends 页）：卡片=图标+名称+类型+框架+版本；点击卡片→版本详情弹窗 */
export default function InferBackends() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [kw, setKw] = useState('')
  const [src, setSrc] = useState<string | undefined>(undefined)
  const [openDetail, setOpenDetail] = useState<any>(null)  // 版本详情弹窗：当前后端
  const [verFw, setVerFw] = useState<string | undefined>(undefined)
  const [openNew, setOpenNew] = useState(false)
  const [openEdit, setOpenEdit] = useState<any>(null)
  const [openVer, setOpenVer] = useState<any>(null)  // {backend, ver?}
  const [newForm] = Form.useForm()
  const [editForm] = Form.useForm()
  const [verForm] = Form.useForm()
  const [importing, setImporting] = useState(false)

  const load = useCallback(() => {
    api.get('/api/inference/backends').then((r) => setList(r.data || [])).catch(() => {})
  }, [])
  useEffect(() => { load(); const x = setInterval(load, 5000); return () => clearInterval(x) }, [load])

  // ---------- JSON 输入辅助（env/params 以 JSON 文本编辑，提交前解析） ----------
  const parseJsonField = (raw: string | undefined, label: string): Record<string, any> => {
    if (!raw || !raw.trim()) return {}
    try {
      const o = JSON.parse(raw)
      if (o && typeof o === 'object' && !Array.isArray(o)) return o
      message.error(`${label}: ${t('backends.jsonInvalid')}`)
      return null as any
    } catch {
      message.error(`${label}: ${t('backends.jsonInvalid')}`)
      return null as any
    }
  }
  const jsonOf = (o: any) => (o && Object.keys(o).length ? JSON.stringify(o, null, 2) : '')

  // ---------- 自定义后端 创建 / 编辑 ----------
  const submitNew = async () => {
    const v = await newForm.validateFields()
    const env = parseJsonField(v.default_env, 'default_env')
    if (env === null) return
    const params = parseJsonField(v.default_params, 'default_params')
    if (params === null) return
    await api.post('/api/inference/backends', {
      name: v.name, description: v.description || '',
      health_check_path: v.health_check_path || '/v1/models',
      default_env: env, default_params: params,
    })
    message.success(t('backends.created'))
    setOpenNew(false)
    load()
  }

  const submitEdit = async () => {
    const b = openEdit
    const v = await editForm.validateFields()
    const env = parseJsonField(v.default_env, 'default_env')
    if (env === null) return
    const params = parseJsonField(v.default_params, 'default_params')
    if (params === null) return
    await api.put(`/api/inference/backends/${b.id}`, {
      description: v.description || '',
      health_check_path: v.health_check_path || '/v1/models',
      default_env: env, default_params: params,
    })
    message.success(t('backends.saved'))
    setOpenEdit(null)
    load()
  }

  const delBackend = (b: any) => {
    api.delete(`/api/inference/backends/${b.id}`).then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  // ---------- 版本管理 ----------
  const openAddVer = (b: any) => {
    verForm.resetFields()
    verForm.setFieldsValue({ version: '', image: '', framework: 'cuda', entrypoint: '', run_command: '', env: '', is_default: false })
    setOpenVer({ backend: b })
  }
  const openEditVer = (b: any, ver: any) => {
    verForm.resetFields()
    verForm.setFieldsValue({
      version: ver.version, image: ver.image, framework: ver.framework,
      entrypoint: ver.entrypoint, run_command: ver.run_command,
      env: jsonOf(ver.env), is_default: ver.is_default,
    })
    setOpenVer({ backend: b, ver })
  }
  const submitVer = async () => {
    const { backend: b, ver } = openVer
    const v = await verForm.validateFields()
    const env = parseJsonField(v.env, 'env')
    if (env === null) return
    if (ver) {
      await api.put(`/api/inference/backends/${b.id}/versions/${ver.id}`, {
        version: v.version, image: v.image, framework: v.framework || 'cuda',
        entrypoint: v.entrypoint || '', run_command: v.run_command || '',
        env, is_default: !!v.is_default,
      })
    } else {
      await api.post(`/api/inference/backends/${b.id}/versions`, {
        version: v.version, image: v.image, framework: v.framework || 'cuda',
        entrypoint: v.entrypoint || '', run_command: v.run_command || '',
        env, is_default: !!v.is_default,
      })
    }
    message.success(t('backends.saved'))
    setOpenVer(null)
    load()
  }
  const setDefaultVer = (b: any, ver: any) => {
    api.put(`/api/inference/backends/${b.id}/versions/${ver.id}`, { is_default: true })
      .then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }
  const delVer = (b: any, ver: any) => {
    api.delete(`/api/inference/backends/${b.id}/versions/${ver.id}`).then(() => load())
      .catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  // ---------- YAML 导入 / 导出 ----------
  const doImport = async (file: File) => {
    setImporting(true)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const r = await api.post('/api/inference/backends/import', fd)
      message.success(t('backends.imported', { n: r.data.created }))
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || e.message)
    } finally {
      setImporting(false)
    }
    return false  // 不走 antd 自动上传
  }
  const doExport = (b: any) => {
    api.get(`/api/inference/backends/${b.id}/export`, { responseType: 'blob' }).then((r) => {
      const url = URL.createObjectURL(new Blob([r.data]))
      const a = document.createElement('a')
      a.href = url
      a.download = `${b.name}.yaml`
      a.click()
      URL.revokeObjectURL(url)
    }).catch((e) => message.error(e.response?.data?.detail || e.message))
  }

  const copy = (text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => message.success(t('infer.copied')),
      () => message.error(t('infer.copy')),
    )
  }

  // ---------- 卡片操作 ----------
  const cardActions = (b: any) => [
    { key: 'view', icon: <SearchOutlined />, label: t('backends.viewVersions') },
    { key: 'export', icon: <ImportOutlined />, label: t('backends.exportYaml') },
    ...(b.source === 'custom' ? [
      { type: 'divider' as const },
      { key: 'addVer', icon: <PlusOutlined />, label: t('backends.addVer') },
      { key: 'edit', icon: <CodeOutlined />, label: t('common.edit') },
      { type: 'divider' as const },
      { key: 'delete', icon: <DeleteOutlined />, label: t('common.delete'), danger: true },
    ] : []),
  ]
  const onCardAction = (key: string, b: any) => {
    if (key === 'view') { setVerFw(undefined); setOpenDetail(b); return }
    if (key === 'export') { doExport(b); return }
    if (key === 'addVer') { openAddVer(b); return }
    if (key === 'edit') {
      editForm.resetFields()
      editForm.setFieldsValue({
        description: b.description || '',
        health_check_path: b.health_check_path,
        default_env: jsonOf(b.default_env),
        default_params: jsonOf(b.default_params),
      })
      setOpenEdit(b)
      return
    }
    if (key === 'delete') {
      Modal.confirm({
        title: t('backends.delConfirm'),
        okButtonProps: { danger: true },
        onOk: () => delBackend(b),
      })
    }
  }
  // 版本项操作（详情弹窗内，custom 后端才暴露）
  const onVerAction = (key: string, b: any, ver: any) => {
    if (key === 'def') setDefaultVer(b, ver)
    else if (key === 'edit') openEditVer(b, ver)
    else if (key === 'delete') {
      Modal.confirm({
        title: t('backends.verDelConfirm'),
        okButtonProps: { danger: true },
        onOk: () => delVer(b, ver),
      })
    }
  }

  const filtered = list.filter((b) => {
    const k = kw.trim().toLowerCase()
    if (src && b.source !== src) return false
    return !k || (b.name || '').toLowerCase().includes(k) || (b.description || '').toLowerCase().includes(k)
  })

  // 版本详情弹窗内容（框架筛选）
  const detailVers = openDetail?.versions || []
  const detailFws = [...new Set(detailVers.map((v: any) => v.framework || 'cuda'))]
  const shownVers = verFw ? detailVers.filter((v: any) => (v.framework || 'cuda') === verFw) : detailVers

  return (
    <div>
      {/* 工具栏：左=搜索+来源筛选+刷新；右=YAML 导入+新建 */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 12, flexWrap: 'wrap' }}>
        <Space wrap>
          <Input allowClear prefix={<SearchOutlined style={{ color: 'var(--ant-color-text-quaternary)' }} />}
            placeholder={t('backends.searchPh')} value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 240 }} />
          <Select allowClear placeholder={t('backends.source')} value={src} onChange={(v) => setSrc(v)} style={{ width: 110 }}
            options={[
              { value: 'builtin', label: t('backends.builtin') },
              { value: 'custom', label: t('backends.custom') },
            ]} />
          <Button type="text" icon={<ReloadOutlined />} onClick={load} />
        </Space>
        <Space>
          <Upload accept=".yaml,.yml" showUploadList={false} beforeUpload={doImport} maxCount={1}>
            <Button icon={<ImportOutlined />} loading={importing}>{t('backends.importYaml')}</Button>
          </Upload>
          <Button type="primary" icon={<PlusOutlined />}
            onClick={() => { newForm.resetFields(); newForm.setFieldsValue({ health_check_path: '/v1/models' }); setOpenNew(true) }}>
            {t('backends.new')}
          </Button>
        </Space>
      </div>

      {/* 卡片网格（点击卡片→版本详情） */}
      {filtered.length === 0 ? (
        <Empty description={t('backends.empty')} style={{ padding: '80px 0' }} />
      ) : (
        <Row gutter={[12, 12]}>
          {filtered.map((b) => {
            const def = b.versions.find((v: any) => v.is_default)
            return (
              <Col xs={24} sm={12} md={8} xl={6} key={b.id}>
                <Card size="small" hoverable
                  onClick={() => { setVerFw(undefined); setOpenDetail(b) }}
                  title={
                    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingRight: 8 }}>
                      <Space size={8}>
                        <BackendIcon b={b} />
                        <span style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                          <span style={{ fontWeight: 600, fontSize: 14, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {b.source === 'custom' ? b.name.replace(/-custom$/, '') : b.name}
                          </span>
                          <Tag color={b.source === 'builtin' ? 'geekblue' : 'orange'} style={{ margin: 0, flex: 'none' }}>
                            {b.source === 'builtin' ? t('backends.builtin') : t('backends.custom')}
                          </Tag>
                        </span>
                      </Space>
                      <Dropdown menu={{ items: cardActions(b), onClick: ({ key }) => onCardAction(key, b) }}>
                        <Button size="small" type="text" icon={<MoreOutlined />}
                          onClick={(e) => e.stopPropagation()} />
                      </Dropdown>
                    </div>
                  }
                  extra={null}>
                  {b.description && (
                    <div style={{ fontSize: 12, color: 'var(--ant-color-text-tertiary)', marginBottom: 8,
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                      title={b.description}>{b.description}</div>
                  )}
                  {/* 可用框架（tooltip=对应版本列表） */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
                    <span style={{ fontSize: 12, color: 'var(--ant-color-text-tertiary)', flex: 'none' }}>{t('backends.frameworks')}:</span>
                    {frameworksOf(b).map(({ fw, vers }) => (
                      <Tooltip key={fw} title={vers.join(', ')}>
                        <Tag color={FRAME_COLOR[fw] || 'default'} style={{ margin: 0 }}>{fw}</Tag>
                      </Tooltip>
                    ))}
                  </div>
                  {/* 版本数 · 默认版本 + 生效镜像 */}
                  <div style={{ fontSize: 12, color: 'var(--ant-color-text-secondary)', marginBottom: 6,
                    display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                    <span style={{ flex: 'none' }}>{t('backends.versions')} {b.versions.length}</span>
                    {def && (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, minWidth: 0 }}>
                        <span>·</span>
                        <span style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{def.version}</span>
                        <StarOutlined style={{ color: '#faad14', flex: 'none' }} />
                      </span>
                    )}
                  </div>
                  {b.current_image && (
                    <Tooltip title={t('backends.currentImageHint')}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0 }}>
                        <code style={{ fontSize: 11, flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {b.current_image}
                        </code>
                        <Button size="small" type="text" icon={<CopyOutlined />} style={{ flex: 'none' }}
                          onClick={(e) => { e.stopPropagation(); copy(b.current_image) }} />
                      </div>
                    </Tooltip>
                  )}
                </Card>
              </Col>
            )
          })}
        </Row>
      )}

      {/* 版本详情弹窗（点击卡片进入；框架筛选 + 版本项列表；custom 可管理版本） */}
      <Modal open={!!openDetail} width={640} centered destroyOnClose
        onCancel={() => setOpenDetail(null)} footer={null}
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span style={{ fontSize: 14 }}>
              {t('backends.versions')} · {openDetail?.name}（{detailVers.length}）
            </span>
            {openDetail?.source === 'custom' && (
              <Button type="link" size="small" icon={<PlusOutlined />} style={{ padding: 0 }}
                onClick={() => openAddVer(openDetail)}>
                {t('backends.addVer')}
              </Button>
            )}
          </div>
        }>
        <Select allowClear placeholder={t('backends.frameworkFilter')} style={{ width: '100%', marginBottom: 12 }}
          value={verFw} onChange={(v) => setVerFw(v)}
          options={detailFws.map((f) => ({ value: f, label: f }))} />
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12, maxHeight: '60vh', overflow: 'auto', paddingRight: 4 }}>
          {shownVers.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('backends.noVersion')} />
          ) : shownVers.map((v: any) => (
            <div key={v.id} style={{ border: '1px solid var(--ant-color-border)', borderRadius: 10, padding: '10px 14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ fontWeight: 600 }}>{v.version}</span>
                {openDetail?.source === 'builtin' && (
                  <Tag color="geekblue" style={{ margin: 0 }}>{t('backends.builtin')}</Tag>
                )}
                {v.is_default && <Tag color="geekblue" style={{ margin: 0 }}>{t('backends.defaultTag')}</Tag>}
                <span style={{ flex: 1 }} />
                {openDetail?.source === 'custom' && (
                  <Dropdown menu={{
                    items: [
                      ...(!v.is_default ? [{ key: 'def', icon: <StarOutlined />, label: t('backends.setDefault') }] : []),
                      { key: 'edit', icon: <CodeOutlined />, label: t('common.edit') },
                      { type: 'divider' as const },
                      { key: 'delete', icon: <DeleteOutlined />, label: t('common.delete'), danger: true },
                    ],
                    onClick: ({ key }) => onVerAction(key, openDetail, v),
                  }}>
                    <Button size="small" type="text" icon={<MoreOutlined />} />
                  </Dropdown>
                )}
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'max-content minmax(0, 1fr)', gap: '4px 10px', fontSize: 12 }}>
                <span style={{ color: 'var(--ant-color-text-tertiary)' }}>{t('backends.image')}:</span>
                <span style={{ display: 'flex', alignItems: 'center', gap: 4, minWidth: 0 }}>
                  <code style={{ fontSize: 11, wordBreak: 'break-all' }}>{v.image}</code>
                  <Button size="small" type="text" icon={<CopyOutlined />} onClick={() => copy(v.image)} />
                </span>
                {v.entrypoint && (<>
                  <span style={{ color: 'var(--ant-color-text-tertiary)' }}>{t('backends.entrypoint')}:</span>
                  <span style={{ wordBreak: 'break-all' }}>{v.entrypoint}</span>
                </>)}
                {v.run_command && (<>
                  <span style={{ color: 'var(--ant-color-text-tertiary)' }}>{t('backends.runCmd')}:</span>
                  <Tooltip title={v.run_command}>
                    <code style={{ fontSize: 11, display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {v.run_command}
                    </code>
                  </Tooltip>
                </>)}
                {Object.keys(v.env || {}).length > 0 && (<>
                  <span style={{ color: 'var(--ant-color-text-tertiary)' }}>{t('backends.env')}:</span>
                  <Tooltip title={jsonOf(v.env)}>
                    <Tag color="purple" style={{ margin: 0 }}>{t('backends.envKeys', { n: Object.keys(v.env).length })}</Tag>
                  </Tooltip>
                </>)}
                <span style={{ color: 'var(--ant-color-text-tertiary)' }}>framework:</span>
                <Tag color={FRAME_COLOR[v.framework] || 'default'} style={{ margin: 0 }}>{v.framework}</Tag>
              </div>
            </div>
          ))}
        </div>
      </Modal>

      {/* 新建自定义后端 */}
      <Drawer title={t('backends.new')} width={620} open={openNew} onClose={() => setOpenNew(false)}
        footer={<div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => setOpenNew(false)}>{t('common.cancel')}</Button>
            <Button type="primary" onClick={submitNew}>{t('common.confirm')}</Button>
          </Space>
        </div>}>
        <Form form={newForm} layout="vertical">
          <SectionTitle>{t('backends.name')}</SectionTitle>
          <Form.Item name="name"
            rules={[
              { required: true },
              { pattern: /^[a-z0-9-]+$/, message: t('backends.nameRule') },
              { pattern: /-custom$/, message: t('backends.nameSuffix') },
            ]}>
            <Input placeholder={t('backends.namePh')} maxLength={64} showCount />
          </Form.Item>
          <Form.Item name="description" label={t('common.desc')}>
            <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
          </Form.Item>
          <SectionTitle>{t('backends.healthPath')}</SectionTitle>
          <Form.Item name="health_check_path" tooltip={t('backends.healthPathHint')}>
            <Input placeholder="/v1/models" />
          </Form.Item>
          <SectionTitle>{t('backends.defaultEnv')}</SectionTitle>
          <Form.Item name="default_env" tooltip={t('backends.jsonInvalidHint')}>
            <Input.TextArea placeholder='{"SOME_KEY": "value"}' autoSize={{ minRows: 1, maxRows: 4 }} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
          <Form.Item name="default_params" label={t('backends.defaultParams')} tooltip={t('backends.jsonInvalidHint')}>
            <Input.TextArea placeholder='{"max_model_len": 8192}' autoSize={{ minRows: 1, maxRows: 4 }} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Drawer>

      {/* 编辑自定义后端（名不可改） */}
      <Drawer title={openEdit ? `${t('common.edit')} · ${openEdit.name}` : t('common.edit')}
        width={620} open={!!openEdit} onClose={() => setOpenEdit(null)}
        footer={<div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => setOpenEdit(null)}>{t('common.cancel')}</Button>
            <Button type="primary" onClick={submitEdit}>{t('common.confirm')}</Button>
          </Space>
        </div>}>
        <Form form={editForm} layout="vertical">
          <Form.Item name="description" label={t('common.desc')}>
            <Input.TextArea maxLength={200} showCount autoSize={{ minRows: 1, maxRows: 3 }} />
          </Form.Item>
          <Form.Item name="health_check_path" label={t('backends.healthPath')} tooltip={t('backends.healthPathHint')}>
            <Input placeholder="/v1/models" />
          </Form.Item>
          <Form.Item name="default_env" label={t('backends.defaultEnv')} tooltip={t('backends.jsonInvalidHint')}>
            <Input.TextArea placeholder='{"SOME_KEY": "value"}' autoSize={{ minRows: 1, maxRows: 4 }} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
          <Form.Item name="default_params" label={t('backends.defaultParams')} tooltip={t('backends.jsonInvalidHint')}>
            <Input.TextArea placeholder='{"max_model_len": 8192}' autoSize={{ minRows: 1, maxRows: 4 }} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
        </Form>
      </Drawer>

      {/* 版本 创建/编辑（custom 后端） */}
      <Drawer title={openVer?.ver
        ? `${t('backends.verEdit', { v: openVer.ver.version, b: openVer.backend.name })}`
        : t('backends.addVerFor', { b: openVer?.backend?.name })}
        width={620} open={!!openVer} onClose={() => setOpenVer(null)}
        footer={<div style={{ textAlign: 'right' }}>
          <Space>
            <Button onClick={() => setOpenVer(null)}>{t('common.cancel')}</Button>
            <Button type="primary" onClick={submitVer}>{t('common.confirm')}</Button>
          </Space>
        </div>}>
        <Form form={verForm} layout="vertical">
          <SectionTitle>{t('backends.ver')}</SectionTitle>
          <Form.Item name="version"
            rules={[{ required: true, pattern: /^[a-zA-Z0-9._-]+$/, message: t('backends.verRule') }]}>
            <Input maxLength={64} placeholder="v0.1" />
          </Form.Item>
          <Form.Item name="framework" label="framework">
            <Select options={['cuda', 'rocm', 'cpu'].map((f) => ({ value: f, label: f }))} />
          </Form.Item>
          <SectionTitle>{t('backends.image')}</SectionTitle>
          <Form.Item name="image" rules={[{ required: true }]}>
            <Input placeholder="registry.example.com/xxx:v1" />
          </Form.Item>
          <Form.Item name="entrypoint" label={t('backends.entrypoint')} tooltip={t('backends.entrypointHint')}>
            <Input placeholder="/usr/bin/python" />
          </Form.Item>
          <Form.Item name="run_command" label={t('backends.runCmd')}
            tooltip={t('backends.runCmdHint')}
            rules={[{ required: openVer?.backend?.source === 'custom', message: t('backends.runCmdReq') }]}>
            <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} style={{ fontFamily: 'monospace', fontSize: 12 }}
              placeholder={'python3 -m xxx --model {{model_path}} --port {{port}}'} />
          </Form.Item>
          <Form.Item name="env" label={t('backends.env')} tooltip={t('backends.jsonInvalidHint')}>
            <Input.TextArea placeholder='{"EXTRA_KEY": "value"}' autoSize={{ minRows: 1, maxRows: 4 }} style={{ fontFamily: 'monospace', fontSize: 12 }} />
          </Form.Item>
          <Form.Item name="is_default" label={t('backends.defaultVer')} valuePropName="checked" tooltip={t('backends.defaultVerHint')}>
            <Switch checkedChildren={t('backends.yesDefault')} unCheckedChildren={t('backends.noDefault')} />
          </Form.Item>
        </Form>
      </Drawer>
    </div>
  )
}
