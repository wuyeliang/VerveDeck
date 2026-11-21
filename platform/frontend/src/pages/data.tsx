import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, Select, Upload, Checkbox,
  Drawer, Tabs, Progress, message, Radio, Row, Col,
} from 'antd'
import { PlusOutlined, EyeOutlined, CloudUploadOutlined, ThunderboltOutlined, DownloadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'
import { useStatusLabel } from '../status'

const T_STATUS: Record<string, string> = { draft: 'default', committed: 'success', running: 'processing', done: 'success', failed: 'error', pending: 'default' }
const FMTS = ['alpaca', 'sharegpt', 'dpo']

export default function DataCenter() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [sel, setSel] = useState<number[]>([])
  const [q, setQ] = useState('')
  const [cur, setCur] = useState<any>(null)
  const [drawer, setDrawer] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [tasks, setTasks] = useState<any[]>([])
  const [deps, setDeps] = useState<any[]>([])
  const [createForm] = Form.useForm()
  // 新建：导入方式
  const [createTab, setCreateTab] = useState('local')
  const [upFile, setUpFile] = useState<any>(null)
  const [upFmt, setUpFmt] = useState('alpaca')
  const [s3Key, setS3Key] = useState('')
  const [s3Fmt, setS3Fmt] = useState('alpaca')
  // 存储位置选项（注册后端，仅 id/name/type；任意登录用户可读）
  const [backends, setBackends] = useState<any[]>([])
  useEffect(() => {
    api.get('/api/storage/backends/options').then((r) => setBackends(r.data || [])).catch(() => setBackends([]))
  }, [])
  // 后端类型友好名（复用存储页 type* 键）
  const typeLabel: Record<string, string> = {
    object: t('storage.typeObject'), local: t('storage.typeLocal'), nfs: t('storage.typeNfs'),
    block: t('storage.typeBlock'), juicefs: t('storage.typeJuicefs'),
  }

  const load = useCallback(() => {
    api.get('/api/datasets', { params: { project_id: projectId, q } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId, q])
  useEffect(() => { load() }, [load])
  const batchDel = useBatchDelete('/api/datasets/batch-delete', () => { setSel([]); load() })

  const openDs = (id: number) => {
    setDrawer(true)
    setCur(null)
    api.get(`/api/datasets/${id}`).then((r) => setCur(r.data)).catch(() => {})
    if (projectId) api.get('/api/inference/deployments', { params: { project_id: projectId } })
      .then((r) => setDeps(r.data.filter((d) => d.status === 'running'))).catch(() => {})
  }
  const reloadCur = () => {
    if (cur) api.get(`/api/datasets/${cur.id}`).then((r) => setCur(r.data)).catch(() => {})
  }
  const loadTasks = () => {
    api.get('/api/datasets/tasks', { params: cur ? { dataset_id: cur.id } : {} }).then((r) => setTasks(r.data)).catch(() => {})
  }
  useEffect(() => {
    if (!drawer) return
    const t = setInterval(() => { loadTasks() }, 5000)
    return () => clearInterval(t)
  }, [drawer, cur?.id])
  useEffect(() => { if (drawer) { loadTasks(); reloadCur() } }, [drawer])

  const openCreate = () => {
    createForm.resetFields()
    setCreateTab('local'); setUpFile(null); setS3Key(''); setUpFmt('alpaca'); setS3Fmt('alpaca')
    setCreateOpen(true)
  }

  // 模板下载（blob + 本地 objectURL）
  const downloadTemplate = (fmt: string) => {
    api.get('/api/datasets/template', { params: { fmt }, responseType: 'blob' }).then((r) => {
      const url = URL.createObjectURL(new Blob([r.data]))
      const a = document.createElement('a')
      a.href = url; a.download = `dataset_${fmt}_template.jsonl`
      a.click()
      URL.revokeObjectURL(url)
    }).catch(() => {})
  }

  const onCreate = async () => {
    try { await createForm.validateFields() } catch { return }
    const v = createForm.getFieldsValue()
    const sbid = v.storage_backend_id === undefined || v.storage_backend_id === '' || v.storage_backend_id === null
      ? null : Number(v.storage_backend_id)  // 0=内置对象存储，须保留（不可 || null 吞掉）
    const r = await api.post('/api/datasets', { project_id: projectId, ...v,
      storage_backend_id: sbid })
    const did = r.data.id
    try {
      if (createTab === 'local' && upFile) {
        const fd = new FormData()
        fd.append('file', upFile)
        await api.post(`/api/datasets/${did}/versions/upload?fmt=${upFmt}&note=upload:${upFile.name}`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
      } else if (createTab === 's3' && s3Key.trim()) {
        await api.post('/api/datasets/import-s3', { dataset_id: did, key: s3Key.trim(), fmt: s3Fmt })
      }
    } catch { /* 数据集已创建；导入失败在详情里重试即可 */ }
    message.success(t('data.created'))
    setCreateOpen(false)
    load()
  }

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('data.title')}</h3>
        <Space wrap>
          <Input allowClear placeholder={t('common.search')} value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 220 }} />
          {sel.length > 0 && <Button danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t('data.newDataset')}</Button>
        </Space>
      </Space>
      <Table rowKey="id" dataSource={list} pagination={{ pageSize: 10 }}
        rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name', width: 180, ellipsis: true },
          { title: t('data.dsKind'), dataIndex: 'kind', width: 90,
            render: (v: string) => <Tag color={v === 'eval' ? 'purple' : 'blue'}>{v === 'eval' ? t('data.kindEval') : t('data.kindTrain')}</Tag> },
          { title: t('data.colStorage'), dataIndex: 'storage_backend', width: 130, ellipsis: true,
            render: (v: string) => v
              ? <Tag color="cyan" style={{ fontSize: 11 }}>{v}</Tag>
              : <Tag style={{ fontSize: 11 }}>{t('data.storageDefault')}</Tag> },
          { title: t('data.colLatest'), dataIndex: 'latest_version', width: 90, render: (v: number) => (v ? `v${v}` : '-') },
          { title: t('data.colVolume'), dataIndex: 'samples', width: 80 },
          { title: t('data.colPublished'), dataIndex: 'published', width: 100,
            render: (v: boolean) => <Tag color={v ? 'success' : 'default'}>{v ? t('data.published') : t('data.unpublished')}</Tag> },
          { title: t('common.desc'), dataIndex: 'description', ellipsis: true },
          { title: t('common.createTime'), dataIndex: 'created_at', width: 170 },
          {
            title: t('common.actions'), width: 150, render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" icon={<EyeOutlined />} onClick={() => openDs(r.id)}>{t('common.detail')}</Button>
                <Button size="small" danger onClick={() => Modal.confirm({
                  title: t('common.deleteConfirm'), okText: t('common.delete'), cancelText: t('common.cancel'),
                  onOk: async () => { await api.delete(`/api/datasets/${r.id}`); message.success(t('common.deleted')); load() },
                })}>{t('common.delete')}</Button>
              </Space>
            ),
          },
        ]} />

      {/* 新建数据集（类型/场景/导入方式/模板下载） */}
      <Modal title={t('data.newDataset')} open={createOpen} onCancel={() => setCreateOpen(false)}
        okText={t('common.create')} onOk={onCreate} width={640}
        afterClose={() => createForm.resetFields()}>
        <Form form={createForm} layout="vertical" initialValues={{ name: '', description: '', kind: 'train', scenario: 'text' }}>
          <Form.Item name="name" label={t('common.name')} rules={[{ required: true, message: t('data.requiredName') }, { max: 50 }]}>
            <Input maxLength={50} showCount placeholder={t('data.namePh')} />
          </Form.Item>
          <Form.Item name="description" label={t('common.desc')}>
            <Input.TextArea rows={2} maxLength={200} showCount />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}><Form.Item name="kind" label={t('data.dsKind')}>
              <Radio.Group options={[{ value: 'train', label: t('data.kindTrain') }, { value: 'eval', label: t('data.kindEval') }]} />
            </Form.Item></Col>
            <Col span={12}><Form.Item name="scenario" label={t('data.scenario')}>
              <Radio.Group options={[{ value: 'text', label: t('data.sceneText') }, { value: 'vlm', label: t('data.sceneVlm') }]} />
            </Form.Item></Col>
          </Row>
          {backends.length > 0 && (
            <Form.Item name="storage_backend_id" label={t('data.storageLocation')}
              tooltip={<span style={{ whiteSpace: 'pre-line' }}>{t('data.storageLocationTip')}</span>}>
              <Select allowClear options={[
                { value: '', label: t('data.storageDefault') },
                ...backends.map((b) => ({ value: b.id, label: `${b.name}（${typeLabel[b.type] || b.type}）` })),
              ]} />
            </Form.Item>
          )}
          <Form.Item label={t('data.importMethod')} required style={{ marginBottom: 0 }}>
            <Tabs activeKey={createTab} onChange={setCreateTab} size="small" items={[
              {
                key: 'local', label: t('data.tabLocal'), children: (
                  <div>
                    <Upload.Dragger accept=".json,.jsonl" showUploadList={false}
                      beforeUpload={(f) => { setUpFile(f); return false }}>
                      <p className="ant-upload-drag-icon"><CloudUploadOutlined /></p>
                      <p>{upFile ? upFile.name : t('data.uploadVersion')}</p>
                    </Upload.Dragger>
                    <Space style={{ marginTop: 8 }}>
                      <Select value={upFmt} onChange={setUpFmt} style={{ width: 140 }} options={FMTS.map((f) => ({ value: f, label: f }))} />
                      <span style={{ color: '#888' }}>{t('data.importLocalHint')}</span>
                    </Space>
                  </div>
                ),
              },
              {
                key: 's3', label: t('data.tabOss'), children: (
                  <div>
                    <Input value={s3Key} onChange={(e) => setS3Key(e.target.value)} placeholder={t('data.ossKeyPh')} />
                    <Space style={{ marginTop: 8 }}>
                      <Select value={s3Fmt} onChange={setS3Fmt} style={{ width: 140 }} options={FMTS.map((f) => ({ value: f, label: f }))} />
                      <span style={{ color: '#888' }}>{t('data.importOssHint')}</span>
                    </Space>
                  </div>
                ),
              },
            ]} />
          </Form.Item>
          <div style={{ marginTop: 4, color: '#555' }}>
            <DownloadOutlined /> {t('data.templateBtn')}：
            {FMTS.map((f) => (
              <a key={f} style={{ marginLeft: 10 }} onClick={() => downloadTemplate(f)}>{f}</a>
            ))}
          </div>
        </Form>
      </Modal>

      <Drawer title={cur ? t('data.drawerTitleWith', { name: cur.name }) : t('data.drawerTitle')} width={860} open={drawer} onClose={() => setDrawer(false)}
        extra={cur && (
          <Space>
            <Tag color={cur.kind === 'eval' ? 'purple' : 'blue'}>{cur.kind === 'eval' ? t('data.kindEval') : t('data.kindTrain')}</Tag>
            <Tag>{cur.scenario === 'vlm' ? t('data.sceneVlm') : t('data.sceneText')}</Tag>
            {cur.storage_backend
              ? <Tag color="cyan">{t('data.colStorage')}: {cur.storage_backend}</Tag>
              : <Tag>{t('data.storageDefault')}</Tag>}
          </Space>
        )}>
        {cur && (
          <DsDetail cur={cur} projectId={projectId} reload={reloadCur} deps={deps}
            onCreateVersion={(fmt, note, file) => {
              const fd = new FormData()
              fd.append('file', file)
              api.post(`/api/datasets/${cur.id}/versions/upload?fmt=${fmt}&note=${encodeURIComponent(note)}`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
                .then(() => { message.success(t('data.versionCreated')); reloadCur() })
            }}
            onParse={(file) => {
              const fd = new FormData()
              fd.append('file', file)
              api.post('/api/datasets/parse?dataset_id=' + cur.id, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
                .then((r) => { message.success(t('data.parseSubmitted', { id: r.data.task_id })) })
            }}
            onClean={(p) => api.post('/api/datasets/clean', { dataset_id: cur.id, ...p }).then((r) => message.success(t('data.cleanSubmitted', { id: r.data.task_id })))}
            onSynth={(p) => api.post('/api/datasets/synth', { dataset_id: cur.id, ...p }).then((r) => message.success(t('data.synthSubmitted', { id: r.data.task_id })))}
            onAnnotate={(vid) => api.post(`/api/datasets/${cur.id}/annotate`, { version_id: vid }).then((r) => {
              Modal.info({ title: t('data.argillaTitle'), width: 480, content: <a href={r.data.argilla_url} target="_blank" rel="noreferrer">{r.data.argilla_url}</a> })
            })}
            onCommit={(vid) => api.post(`/api/datasets/${cur.id}/versions/${vid}/commit`).then(() => { message.success(t('data.dvcCommitted')); reloadCur() })}
            tasks={tasks}
          />
        )}
      </Drawer>
    </div>
  )
}

function DsDetail(props: any) {
  const { t } = useTranslation()
  const st = useStatusLabel()
  const { cur, projectId, reload, deps, tasks } = props
  const [pv, setPv] = useState<any>(null)
  const [pvPage, setPvPage] = useState(1)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadFmt, setUploadFmt] = useState('alpaca')
  const [uploadNote, setUploadNote] = useState('')
  const [cleanForm] = Form.useForm()
  const [synthForm] = Form.useForm()

  const preview = (vid: number) => {
    setPv({ vid, items: [], total: 0 })
    api.get(`/api/datasets/${cur.id}/versions/${vid}/samples`, { params: { page: pvPage, size: 10 } })
      .then((r) => setPv({ vid, items: r.data.items, total: r.data.total }))
  }

  return (
    <Tabs items={[
      {
        key: 'versions', label: t('data.tabVersions'), children: (
          <div>
            <Space style={{ marginBottom: 12 }}>
              <Button type="primary" onClick={() => setUploadOpen(true)}>{t('data.uploadVersion')}</Button>
              <Tag color="blue">{t('data.dvcTag')}</Tag>
            </Space>
            <Table size="small" rowKey="id" dataSource={cur.versions || []} pagination={false}
              columns={[
                { title: t('data.colVersion'), dataIndex: 'version', width: 60, render: (v: number) => `v${v}` },
                { title: t('data.colFmt'), dataIndex: 'fmt', width: 90 },
                { title: t('data.colSamples'), dataIndex: 'sample_count', width: 80 },
                { title: t('data.colSizeKB'), width: 90, render: (_: any, r: any) => (r.size_bytes / 1024).toFixed(1) },
                { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={T_STATUS[v]}>{st(v)}</Tag> },
                { title: t('data.colDvcRev'), dataIndex: 'dvc_rev', width: 110, ellipsis: true },
                { title: t('data.colNote'), dataIndex: 'note', ellipsis: true },
                {
                  title: t('common.actions'), width: 220, render: (_: any, r: any) => (
                    <Space size={4}>
                      <Button size="small" onClick={() => preview(r.id)}>{t('data.previewBtn')}</Button>
                      {r.status === 'draft' && <Button size="small" onClick={() => props.onCommit(r.id)}>{t('data.dvcCommit')}</Button>}
                      <Button size="small" onClick={() => props.onAnnotate(r.id)}>{t('data.annotate')}</Button>
                    </Space>
                  ),
                },
              ]} />
          </div>
        ),
      },
      {
        key: 'preview', label: t('data.tabPreview'), children: pv ? (
          <div>
            <div style={{ marginBottom: 8 }}>{t('data.previewMeta', { v: cur.versions.find((v: any) => v.id === pv.vid)?.version, n: pv.total })}</div>
            <Table size="small" rowKey={(_, i) => String(i)} dataSource={pv.items}
              pagination={{ current: pvPage, pageSize: 10, total: pv.total, onChange: (p) => { setPvPage(p); preview(pv.vid) } }}
              columns={[
                { title: 'instruction', dataIndex: 'instruction', ellipsis: true, width: 300 },
                { title: 'input', dataIndex: 'input', ellipsis: true, width: 150 },
                { title: 'output', dataIndex: 'output', ellipsis: true },
              ]} />
          </div>
        ) : <div>{t('data.previewHint')}</div>,
      },
      {
        key: 'parse', label: t('data.tabParse'), children: (
          <div>
            <p>{t('data.parseIntro')}</p>
            <Upload.Dragger accept=".pdf,.doc,.docx,.ppt,.pptx,.md,.txt" showUploadList={false}
              beforeUpload={(f) => { props.onParse(f); return false }}>
              <p className="ant-upload-drag-icon"><CloudUploadOutlined /></p>
              <p>{t('data.parseDrop')}</p>
            </Upload.Dragger>
          </div>
        ),
      },
      {
        key: 'clean', label: t('data.tabClean'), children: (
        <Form form={cleanForm} layout="vertical" style={{ maxWidth: 520 }}
          initialValues={{ pii: ['phone', 'idcard', 'email', 'bankcard'], dedup: true, min_len: 0 }}
          onFinish={(v) => props.onClean({ source_version_id: v.source_version_id, pii: v.pii, min_len: v.min_len, dedup: v.dedup, custom_regex: [] })}>
          <Form.Item name="source_version_id" label={t('data.srcVersion')} rules={[{ required: true }]}>
            <Select options={(cur.versions || []).map((v: any) => ({ value: v.id, label: `v${v.version} (${v.sample_count}, ${v.status})` }))} />
          </Form.Item>
          <Form.Item name="pii" label={t('data.pii')}><Checkbox.Group options={['phone', 'idcard', 'email', 'bankcard']} /></Form.Item>
          <Form.Item name="min_len" label={t('data.minLen')}><Input type="number" /></Form.Item>
          <Form.Item name="dedup" valuePropName="checked"><Checkbox>{t('data.dedup')}</Checkbox></Form.Item>
          <Button type="primary" htmlType="submit" icon={<ThunderboltOutlined />}>{t('data.genClean')}</Button>
        </Form>
      )},
      {
        key: 'synth', label: t('data.tabSynth'), children: (
        <Form form={synthForm} layout="vertical" style={{ maxWidth: 520 }}
          initialValues={{ mode: 'qa', n_per_chunk: 3 }}
          onFinish={(v) => props.onSynth({ source_version_id: v.source_version_id, mode: v.mode, n_per_chunk: v.n_per_chunk, deployment_id: v.deployment_id })}>
          <Form.Item name="source_version_id" label={t('data.synthSrc')} rules={[{ required: true }]}>
            <Select options={(cur.versions || []).map((v: any) => ({ value: v.id, label: `v${v.version} (${v.status})` }))} />
          </Form.Item>
          <Form.Item name="mode" label={t('data.synthMode')}><Select options={[{ value: 'qa', label: t('data.modeQa') }, { value: 'cot', label: t('data.modeCot') }]} /></Form.Item>
          <Form.Item name="n_per_chunk" label={t('data.nPerChunk')}><Input type="number" /></Form.Item>
          <Form.Item name="deployment_id" label={t('data.genModel')} rules={[{ required: true }]}>
            <Select options={deps.map((d: any) => ({ value: d.id, label: `${d.name} (${d.model})` }))} placeholder={t('data.needDeploy')} />
          </Form.Item>
          <Button type="primary" htmlType="submit" icon={<ThunderboltOutlined />}>{t('data.startSynth')}</Button>
        </Form>
      )},
      {
        key: 'tasks', label: t('data.tabTasks', { n: tasks.length }), children: (
          <Table size="small" rowKey="id" dataSource={tasks} pagination={{ pageSize: 8 }}
            columns={[
              { title: t('common.id'), dataIndex: 'id', width: 55 },
              { title: t('data.colType'), dataIndex: 'kind', width: 80 },
              { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={T_STATUS[v] || 'default'}>{st(v)}</Tag> },
              { title: t('data.colProgress'), dataIndex: 'progress', width: 120, render: (v: number, r: any) => r.kind === 'synth' ? <Progress percent={v} size="small" /> : '-' },
              { title: t('data.colFile'), render: (_: any, r: any) => r.file_name || JSON.stringify(r.result || {}), ellipsis: true },
              { title: t('data.colError'), dataIndex: 'error', ellipsis: true },
            ]} />
        ),
      },
  ]} />
  )
}
