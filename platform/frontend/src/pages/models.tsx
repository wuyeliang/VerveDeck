import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, Select, Drawer, Descriptions, Timeline, message, Empty,
} from 'antd'
import { PlusOutlined, NodeIndexOutlined, DownloadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

const TYPE_COLOR: Record<string, string> = { base: 'geekblue', lora: 'purple', merged: 'green' }
const ST_COLOR: Record<string, string> = { ready: 'success', pending_download: 'processing', failed: 'error' }

export default function ModelRepo() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState(false)
  const [lineage, setLineage] = useState<any>(null)
  const [form] = Form.useForm()

  const load = useCallback(() => {
    api.get('/api/models', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t) }, [load])

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <h3 style={{ margin: 0 }}>{t('models.title')}</h3>
        <Space>
          <Select value={filter} onChange={setFilter} style={{ width: 140 }}
            options={[{ value: '', label: t('models.filterAll') }, { value: 'base', label: t('models.base') }, { value: 'lora', label: t('models.lora') }, { value: 'merged', label: t('models.merged') }]} />
          <Button type="primary" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setOpen(true) }}>{t('models.registerBase')}</Button>
        </Space>
      </Space>
      <Table rowKey="id" size="small" dataSource={list.filter((m) => !filter || m.type === filter)} pagination={{ pageSize: 12 }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('common.type'), dataIndex: 'type', width: 90, render: (v: string) => <Tag color={TYPE_COLOR[v]}>{v}</Tag> },
          { title: t('models.colSource'), render: (_: any, r: any) => r.source || r.hf_id || '-', width: 200, ellipsis: true },
          { title: t('common.status'), dataIndex: 'status', width: 130, render: (v: string) => <Tag color={ST_COLOR[v]} icon={v === 'pending_download' ? <DownloadOutlined spin /> : undefined}>{v}</Tag> },
          { title: t('models.colSize'), width: 90, render: (_: any, r: any) => (r.size_bytes / 1073741824).toFixed(2) },
          { title: t('models.lineage'), width: 90, render: (_: any, r: any) => r.job_id || r.parent_name ? <Button size="small" icon={<NodeIndexOutlined />} onClick={() => api.get(`/api/models/${r.id}/lineage`).then((x) => setLineage(x.data))}>{t('models.trace')}</Button> : '-' },
          { title: t('common.createTime'), dataIndex: 'created_at', width: 170 },
        ]} />

      <Modal title={t('models.modalTitle')} open={open} onCancel={() => setOpen(false)} width={520}
        onOk={async () => {
          const v = await form.validateFields()
          await api.post('/api/models/base', { project_id: projectId, ...v })
          message.success(t('models.downloadSubmitted')); setOpen(false); load()
        }}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('models.nameInRepo')} rules={[{ required: true }]}><Input placeholder={t('models.namePh')} /></Form.Item>
          <Form.Item name="hf_id" label={t('models.hfId')} rules={[{ required: true }]}><Input placeholder={t('models.hfIdPh')} /></Form.Item>
          <Form.Item name="note" label={t('models.note')}><Input /></Form.Item>
        </Form>
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
