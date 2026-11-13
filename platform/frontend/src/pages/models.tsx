import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, Select, Drawer, Descriptions, Timeline,
  message, Empty, Tabs, Upload,
} from 'antd'
import {
  PlusOutlined, NodeIndexOutlined, DownloadOutlined, SearchOutlined, InboxOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const TYPE_COLOR: Record<string, string> = { base: 'geekblue', lora: 'purple', merged: 'green' }
const ST_COLOR: Record<string, string> = { ready: 'success', pending_download: 'processing', failed: 'error' }
const fmtGB = (b: number) => (b / 1073741824).toFixed(2)

export default function ModelRepo() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState('search')
  const [lineage, setLineage] = useState<any>(null)
  const [form] = Form.useForm()
  // 搜索
  const [src, setSrc] = useState<'hf' | 'ms'>('hf')
  const [kw, setKw] = useState('')
  const [results, setResults] = useState<any[]>([])
  const [searching, setSearching] = useState(false)
  // 上传
  const [upName, setUpName] = useState('')
  const [upNote, setUpNote] = useState('')
  const [upPct, setUpPct] = useState<number | null>(null)

  const load = useCallback(() => {
    api.get('/api/models', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t) }, [load])

  const doSearch = async () => {
    if (!kw.trim()) return
    setSearching(true)
    try {
      const r = await api.get('/api/models/search', { params: { source: src, q: kw.trim(), limit: 30 } })
      setResults(r.data || [])
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    } finally {
      setSearching(false)
    }
  }

  const pickResult = (m: any) => {
    setTab('manual')
    form.setFieldsValue({
      name: m.id.split('/').pop() || m.id,
      hf_id: m.source === 'ms' ? `ms:${m.id}` : m.id,
      note: m.source === 'ms' ? 'modelscope' : 'huggingface',
    })
  }

  const submitManual = async () => {
    const v = await form.validateFields()
    await api.post('/api/models/base', { project_id: projectId, ...v })
    message.success(t('models.downloadSubmitted'))
    closeWizard()
  }

  const closeWizard = () => {
    setOpen(false); setResults([]); setKw(''); setUpPct(null); setUpName(''); setUpNote('')
  }

  const submitUpload = async (file: File) => {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$/.test(upName)) { message.error(t('models.nameRuleUpload')); return false }
    const fd = new FormData()
    fd.append('project_id', String(projectId))
    fd.append('name', upName)
    fd.append('note', upNote)
    fd.append('file', file)
    setUpPct(0)
    try {
      await api.post('/api/models/upload', fd, {
        timeout: 0,
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (e: any) => { if (e.total) setUpPct(Math.round((e.loaded / e.total) * 100)) },
      })
      message.success(t('models.uploadOk'))
      closeWizard(); load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
      setUpPct(null)
    }
    return false
  }

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('models.title')}</h3>
        <Space>
          <Select value={filter} onChange={setFilter} style={{ width: 140 }}
            options={[{ value: '', label: t('models.filterAll') }, { value: 'base', label: t('models.base') }, { value: 'lora', label: t('models.lora') }, { value: 'merged', label: t('models.merged') }]} />
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setTab('search'); setOpen(true) }}>{t('models.registerBase')}</Button>
        </Space>
      </Space>
      <Table rowKey="id" size="small" dataSource={list.filter((m) => !filter || m.type === filter)} pagination={{ pageSize: 12 }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('common.type'), dataIndex: 'type', width: 90, render: (v: string) => <Tag color={TYPE_COLOR[v]}>{v}</Tag> },
          { title: t('models.colSource'), render: (_: any, r: any) => r.source || r.hf_id || '-', width: 200, ellipsis: true },
          {
            title: t('common.status'), dataIndex: 'status', width: 170,
            render: (v: string, r: any) => v === 'pending_download'
              ? <Space size={4}><Tag color="processing" icon={<DownloadOutlined spin />}>{v}</Tag>
                <span style={{ color: '#888', fontSize: 12 }}>{t('models.dlProgress', { mb: Math.round(r.size_bytes / 1048576) })}</span></Space>
              : <Tag color={ST_COLOR[v]}>{v}</Tag>,
          },
          { title: t('models.colSize'), width: 90, render: (_: any, r: any) => r.status === 'pending_download' ? <span style={{ color: '#888' }}>…</span> : fmtGB(r.size_bytes || 0) },
          { title: t('models.lineage'), width: 90, render: (_: any, r: any) => r.job_id || r.parent_name ? <Button size="small" icon={<NodeIndexOutlined />} onClick={() => api.get(`/api/models/${r.id}/lineage`).then((x) => setLineage(x.data))}>{t('models.trace')}</Button> : '-' },
          { title: t('common.createTime'), dataIndex: 'created_at', width: 170 },
          {
            title: t('common.actions'), width: 70, render: (_: any, r: any) => (
              <Button size="small" danger onClick={() => Modal.confirm({
                title: t('common.deleteConfirm'), okText: t('common.delete'), cancelText: t('common.cancel'),
                onOk: async () => { await api.delete(`/api/models/${r.id}`); message.success(t('common.deleted')); load() },
              })}>{t('common.delete')}</Button>
            ),
          },
        ]} />

      {/* 纳管基座模型：搜索（HF/ModelScope）/ 手动填写 / 本地上传 */}
      <Modal title={t('models.modalTitle')} open={open} onCancel={closeWizard} width={760} footer={null}
        destroyOnClose>
        <Tabs activeKey={tab} onChange={setTab} items={[
          {
            key: 'search', label: <span><SearchOutlined /> {t('models.search')}</span>, children: (
              <div>
                <Space style={{ width: '100%', marginBottom: 12 }} wrap>
                  <Select value={src} onChange={setSrc} style={{ width: 160 }}
                    options={[{ value: 'hf', label: t('models.srcHf') }, { value: 'ms', label: t('models.srcMs') }]} />
                  <Input.Search value={kw} onChange={(e) => setKw(e.target.value)} placeholder={t('models.searchPh')}
                    style={{ flex: 1, minWidth: 260 }} loading={searching} onSearch={doSearch}
                    enterButton={t('models.searchBtn')} />
                </Space>
                {results.length === 0 && !searching && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('models.noResults')} />}
                {results.length > 0 && (
                  <Table size="small" rowKey="id" dataSource={results} pagination={false} scroll={{ y: 340 }}
                    columns={[
                      { title: t('models.colModelId'), dataIndex: 'id', ellipsis: true },
                      { title: t('models.downloads'), dataIndex: 'downloads', width: 90, render: (v: number) => v?.toLocaleString?.() ?? 0 },
                      { title: t('common.actions'), width: 80, render: (_: any, r: any) => (
                        <Button size="small" type="primary" ghost onClick={() => pickResult(r)}>{t('models.add')}</Button>
                      )},
                    ]} />
                )}
              </div>
            ),
          },
          {
            key: 'manual', label: t('models.manual'), children: (
              <Form form={form} layout="vertical" onFinish={submitManual}>
                <Form.Item name="name" label={t('models.nameInRepo')} rules={[{ required: true }]}><Input placeholder={t('models.namePh')} /></Form.Item>
                <Form.Item name="hf_id" label={t('models.hfId')} rules={[{ required: true }]}><Input placeholder={t('models.hfIdPh')} /></Form.Item>
                <Form.Item name="note" label={t('models.note')}><Input /></Form.Item>
                <Button type="primary" htmlType="submit">{t('common.create')}</Button>
              </Form>
            ),
          },
          {
            key: 'upload', label: <span><InboxOutlined /> {t('models.upload')}</span>, children: (
              <div>
                <Space style={{ width: '100%', marginBottom: 12 }} wrap>
                  <Input value={upName} onChange={(e) => setUpName(e.target.value)} placeholder={t('models.nameInRepo')} style={{ width: 260 }} />
                  <Input value={upNote} onChange={(e) => setUpNote(e.target.value)} placeholder={t('models.note')} style={{ flex: 1 }} />
                </Space>
                <Upload.Dragger accept=".zip" showUploadList={false} beforeUpload={submitUpload} disabled={upPct != null}>
                  <p className="ant-upload-drag-icon" style={{ margin: 0 }}><InboxOutlined /></p>
                  <p style={{ margin: '4px 0' }}>{t('models.uploadHint')}</p>
                </Upload.Dragger>
                {upPct != null && (
                  <div style={{ marginTop: 12 }}>
                    <Tag color="processing">{t('models.uploading')} {upPct}%</Tag>
                  </div>
                )}
              </div>
            ),
          },
        ]} />
      </Modal>

      <Drawer title={lineage ? t('models.lineageTitle', { name: lineage.asset.name }) : t('models.lineage')} width={620} open={!!lineage} onClose={() => setLineage(null)}>
        {lineage && (
          <div>
            <Descriptions size="small" column={2} bordered style={{ marginBottom: 16 }}>
              <Descriptions.Item label={t('common.type')} span={2}><Tag color={TYPE_COLOR[lineage.asset.type]}>{lineage.asset.type}</Tag></Descriptions.Item>
              <Descriptions.Item label={t('models.path')} span={2}>{lineage.asset.path}</Descriptions.Item>
              <Descriptions.Item label={t('models.colJob')} span={2}>{lineage.asset.job_id ? `#${lineage.asset.job_id}` : '-'}</Descriptions.Item>
              <Descriptions.Item label={t('data.colNote')} span={2}>{lineage.asset.note || '-'}</Descriptions.Item>
            </Descriptions>
            <h4>{t('models.inheritance')}</h4>
            <Timeline items={lineage.chain.map((c: any) => ({
              color: c.type === 'base' ? 'blue' : 'green',
              children: <Space><Tag color={TYPE_COLOR[c.type]}>{c.type}</Tag>{c.name}</Space>,
            }))} />
            <h4>{t('models.children')}</h4>
            {lineage.children.length ? (
              <Table size="small" rowKey="id" dataSource={lineage.children} pagination={false}
                columns={[
                  { title: t('common.name'), dataIndex: 'name' },
                  { title: t('common.type'), dataIndex: 'type', render: (v: string) => <Tag color={TYPE_COLOR[v]}>{v}</Tag> },
                  { title: t('models.colTask'), dataIndex: 'job_id' },
                ]} />
            ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            <h4>{t('models.relatedDs')}</h4>
            {lineage.dataset_versions.length
              ? <Tag key={0}> {lineage.dataset_versions.map((d: any) => `v${d.version} (${d.note || '-'})`).join('  ')}</Tag>
              : <span style={{ color: '#999' }}>{t('common.none')}</span>}
          </div>
        )}
      </Drawer>
    </div>
  )
}
