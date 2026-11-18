import React, { useEffect, useState, useCallback } from 'react'
import {
  Row, Col, Card, Table, Button, Space, Tag, Modal, Form, Input,
  Select, message, Popconfirm, Upload, Tooltip, Switch,
} from 'antd'
import {
  PlusOutlined, EditOutlined, DeleteOutlined, ImportOutlined,
  StarOutlined, CodeOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

/** 推理后端注册表：builtin（vllm/sglang，镜像走 pin 系统）/ custom（版本决定镜像+启动命令模板） */
export default function InferBackends() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
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

  // ---------- YAML 导入 ----------
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

  const copy = (text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => message.success(t('infer.copied')),
      () => message.error(t('infer.copy')),
    )
  }

  const verColumns = (b: any) => [
    { title: t('backends.ver'), dataIndex: 'version', width: 130, render: (v: string, r: any) => (
      <Space size={4}>
        <span style={{ fontWeight: 600 }}>{v}</span>
        {r.is_default && <Tooltip title={t('backends.defaultVer')}><StarOutlined style={{ color: '#faad14' }} /></Tooltip>}
      </Space>
    ) },
    { title: t('backends.image'), dataIndex: 'image', ellipsis: true, render: (v: string) => (
      <Space size={4}><code style={{ fontSize: 11 }}>{v}</code>
        <Button size="small" type="text" icon={<CodeOutlined />} onClick={() => copy(v)} /></Space>
    ) },
    { title: 'framework', dataIndex: 'framework', width: 90 },
    {
      title: t('backends.runCmd'), ellipsis: true, render: (_: any, r: any) => r.run_command
        ? <Tooltip title={r.run_command}><code style={{ fontSize: 11 }}>{r.run_command}</code></Tooltip>
        : <span style={{ color: '#bbb' }}>-</span>,
    },
    {
      title: t('common.actions'), width: 210, render: (_: any, r: any) => (
        <Space size={4}>
          {!r.is_default && (
            <Button size="small" icon={<StarOutlined />} onClick={() => setDefaultVer(b, r)}>{t('backends.setDefault')}</Button>
          )}
          <Button size="small" icon={<EditOutlined />} onClick={() => openEditVer(b, r)}>{t('common.edit')}</Button>
          <Popconfirm title={t('backends.verDelConfirm')} okButtonProps={{ danger: true }} onConfirm={() => delVer(b, r)}>
            <Button size="small" danger icon={<DeleteOutlined />}>{t('common.delete')}</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('backends.title')}</h3>
        <Space>
          <Upload accept=".yaml,.yml" showUploadList={false} beforeUpload={doImport} maxCount={1}>
            <Button icon={<ImportOutlined />} loading={importing}>{t('backends.importYaml')}</Button>
          </Upload>
          <Button type="primary" icon={<PlusOutlined />}
            onClick={() => { newForm.resetFields(); newForm.setFieldsValue({ health_check_path: '/v1/models' }); setOpenNew(true) }}>
            {t('backends.new')}
          </Button>
        </Space>
      </Space>
      <p style={{ color: '#888', fontSize: 12, marginTop: 0 }}>{t('backends.hint')}</p>

      <Row gutter={[12, 12]}>
        {list.map((b: any) => (
          <Col xs={24} xl={12} key={b.id}>
            <Card size="small" title={(
              <Space>
                <span style={{ fontWeight: 700 }}>{b.name}</span>
                <Tag color={b.source === 'builtin' ? 'geekblue' : 'orange'}>
                  {b.source === 'builtin' ? t('backends.builtin') : t('backends.custom')}
                </Tag>
              </Space>
            )}
              extra={(
                <Space size={4}>
                  {b.source === 'custom' && (
                    <>
                      <Button size="small" icon={<EditOutlined />}
                        onClick={() => {
                          editForm.resetFields()
                          editForm.setFieldsValue({
                            description: b.description || '',
                            health_check_path: b.health_check_path,
                            default_env: jsonOf(b.default_env),
                            default_params: jsonOf(b.default_params),
                          })
                          setOpenEdit(b)
                        }}>{t('common.edit')}</Button>
                      <Popconfirm title={t('backends.delConfirm')} okButtonProps={{ danger: true }} onConfirm={() => delBackend(b)}>
                        <Button size="small" danger icon={<DeleteOutlined />}>{t('common.delete')}</Button>
                      </Popconfirm>
                    </>
                  )}
                </Space>
              )}>
              <div style={{ fontSize: 12, color: '#888', marginBottom: 8 }}>
                {b.description || <span style={{ color: '#bbb' }}>-</span>}
              </div>
              <Space size={6} wrap style={{ marginBottom: 8 }}>
                <Tooltip title={t('backends.currentImageHint')}>
                  <Tag icon={<CodeOutlined />} color={b.source === 'builtin' ? 'blue' : 'cyan'}>
                    {b.current_image || '—'}
                  </Tag>
                </Tooltip>
                <Tag>{t('backends.healthPath')}: {b.health_check_path}</Tag>
                {Object.keys(b.default_env || {}).length > 0 && (
                  <Tooltip title={jsonOf(b.default_env)}>
                    <Tag color="purple">{t('backends.envKeys', { n: Object.keys(b.default_env).length })}</Tag>
                  </Tooltip>
                )}
                {Object.keys(b.default_params || {}).length > 0 && (
                  <Tooltip title={jsonOf(b.default_params)}>
                    <Tag color="green">{t('backends.paramKeys', { n: Object.keys(b.default_params).length })}</Tag>
                  </Tooltip>
                )}
              </Space>
              <div style={{ marginBottom: 6, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ fontWeight: 600, fontSize: 13 }}>{t('backends.versions')}（{b.versions.length}）</span>
                {b.source === 'custom' && (
                  <Button size="small" type="dashed" icon={<PlusOutlined />} onClick={() => openAddVer(b)}>
                    {t('backends.addVer')}
                  </Button>
                )}
              </div>
              <Table size="small" rowKey="id" pagination={false} dataSource={b.versions} columns={verColumns(b)}
                scroll={{ x: 560 }} />
            </Card>
          </Col>
        ))}
      </Row>

      {/* 新建自定义后端 */}
      <Modal title={t('backends.new')} open={openNew} onCancel={() => setOpenNew(false)}
        onOk={submitNew} okText={t('common.confirm')} width={620}>
        <Form form={newForm} layout="vertical">
          <Form.Item name="name" label={t('backends.name')}
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
      </Modal>

      {/* 编辑自定义后端（名不可改） */}
      <Modal title={openEdit ? `${t('common.edit')} · ${openEdit.name}` : t('common.edit')}
        open={!!openEdit} onCancel={() => setOpenEdit(null)} onOk={submitEdit} okText={t('common.confirm')} width={620}>
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
      </Modal>

      {/* 版本 创建/编辑（custom 后端） */}
      <Modal title={openVer?.ver
        ? `${t('backends.verEdit', { v: openVer.ver.version, b: openVer.backend.name })}`
        : t('backends.addVerFor', { b: openVer?.backend?.name })}
        open={!!openVer} onCancel={() => setOpenVer(null)} onOk={submitVer} okText={t('common.confirm')} width={680}>
        <Form form={verForm} layout="vertical">
          <Row gutter={12}>
            <Col span={12}>
              <Form.Item name="version" label={t('backends.ver')}
                rules={[{ required: true, pattern: /^[a-zA-Z0-9._-]+$/, message: t('backends.verRule') }]}>
                <Input maxLength={64} placeholder="v0.1" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="framework" label="framework">
                <Select options={['cuda', 'rocm', 'cpu'].map((f) => ({ value: f, label: f }))} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item name="image" label={t('backends.image')} rules={[{ required: true }]}>
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
      </Modal>
    </div>
  )
}
