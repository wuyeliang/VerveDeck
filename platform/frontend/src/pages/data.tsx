import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, Select, Upload, Checkbox,
  Drawer, Tabs, Descriptions, Progress, message, Popconfirm, Divider,
} from 'antd'
import { PlusOutlined, EyeOutlined, CloudUploadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const T_STATUS: Record<string, string> = { draft: 'default', committed: 'success', running: 'processing', done: 'success', failed: 'error', pending: 'default' }

export default function DataCenter() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [cur, setCur] = useState<any>(null)
  const [drawer, setDrawer] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [tasks, setTasks] = useState<any[]>([])
  const [deps, setDeps] = useState<any[]>([])
  const [createForm] = Form.useForm()

  const load = useCallback(() => {
    api.get('/api/datasets', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load() }, [load])

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

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('data.title')}</h3>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>{t('data.newDataset')}</Button>
      </Space>
      <Table rowKey="id" dataSource={list} pagination={{ pageSize: 10 }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 60 },
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('data.colVersions'), dataIndex: 'versions', width: 80 },
          { title: t('common.desc'), dataIndex: 'description', ellipsis: true },
          { title: t('common.createTime'), dataIndex: 'created_at', width: 180 },
          { title: t('common.actions'), width: 100, render: (_: any, r: any) => <Button size="small" icon={<EyeOutlined />} onClick={() => openDs(r.id)}>{t('common.detail')}</Button> },
        ]} />

      <Modal title={t('data.newDataset')} open={createOpen} onCancel={() => setCreateOpen(false)}
        okText={t('common.create')} onOk={async () => {
          try {
            await api.post('/api/datasets', { project_id: projectId, ...createForm.getFieldsValue() })
            message.success(t('data.created')); setCreateOpen(false); load()
          } catch { }
        }}>
        <DsForm form={createForm} />
      </Modal>

      <Drawer title={cur ? t('data.drawerTitleWith', { name: cur.name }) : t('data.drawerTitle')} width={860} open={drawer} onClose={() => setDrawer(false)}>
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

function DsForm({ form }: { form: any }) {
  const { t } = useTranslation()
  return (
    <Form form={form} layout="vertical" initialValues={{ name: '', description: '' }}>
      <Form.Item name="name" label={t('common.name')} rules={[{ required: true, message: t('data.requiredName') }]}><Input placeholder={t('data.namePh')} /></Form.Item>
      <Form.Item name="description" label={t('common.desc')}><Input.TextArea /></Form.Item>
    </Form>
  )
}

function DsDetail(props: any) {
  const { t } = useTranslation()
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
                { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={T_STATUS[v]}>{v}</Tag> },
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
              { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={T_STATUS[v] || 'default'}>{v}</Tag> },
              { title: t('data.colProgress'), dataIndex: 'progress', width: 120, render: (v: number, r: any) => r.kind === 'synth' ? <Progress percent={v} size="small" /> : '-' },
              { title: t('data.colFile'), render: (_: any, r: any) => r.file_name || JSON.stringify(r.result || {}), ellipsis: true },
              { title: t('data.colError'), dataIndex: 'error', ellipsis: true },
            ]} />
        ),
      },
  ]} />
  )
}
