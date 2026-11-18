import React, { useEffect, useState, useCallback } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, Select, Drawer,
  Descriptions, message, Radio,
} from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'

const ST: Record<string, string> = { pending: 'default', running: 'processing', succeeded: 'success', failed: 'error' }

export default function QuantCenter() {
  const { t } = useTranslation()
  const projectId = Number(localStorage.getItem('projectId') || 0)
  const [list, setList] = useState<any[]>([])
  const [sel, setSel] = useState<number[]>([])
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [models, setModels] = useState<any[]>([])
  const [methods, setMethods] = useState<any[]>([])
  const [detail, setDetail] = useState<any>(null)
  const [logs, setLogs] = useState('')

  const load = useCallback(() => {
    api.get('/api/quantize', { params: { project_id: projectId } }).then((r) => setList(r.data)).catch(() => {})
  }, [projectId])
  useEffect(() => { load(); const x = setInterval(load, 6000); return () => clearInterval(x) }, [load])
  const batchDel = useBatchDelete('/api/quantize/batch-delete', () => { setSel([]); load() })

  // 详情日志轮询（打开 Drawer 期间）
  useEffect(() => {
    if (!detail) return
    const fetchLogs = () => api.get(`/api/quantize/${detail.id}/logs`, { params: { tail: 120 } })
      .then((r) => { setLogs(r.data.logs); if (r.data.status !== detail.status) api.get(`/api/quantize/${detail.id}`).then((d) => setDetail(d.data)) })
      .catch(() => {})
    fetchLogs()
    const x = setInterval(fetchLogs, 4000)
    return () => clearInterval(x)
  }, [detail?.id, detail?.status])

  const openWizard = async () => {
    form.resetFields()
    setDetail(null); setLogs('')
    try {
      const [m, ms] = await Promise.all([
        api.get('/api/models', { params: { project_id: projectId } }),
        api.get('/api/quantize/methods'),
      ])
      setModels((m.data || []).filter((x: any) => x.status === 'ready' && x.type !== 'lora'))
      setMethods(ms.data || [])
    } catch { /* ignore */ }
    setOpen(true)
  }

  return (
    <div>
      <Space style={{ marginBottom: 12, width: '100%', justifyContent: 'space-between' }}>
        <div>
          <h3 style={{ margin: 0 }}>{t('quant.title')}</h3>
          <div style={{ color: '#888', fontSize: 12 }}>{t('quant.intro')}</div>
        </div>
        <Space>
          {sel.length > 0 && <Button danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button type="primary" icon={<PlusOutlined />} onClick={openWizard}>{t('quant.newTask')}</Button>
        </Space>
      </Space>

      <Table rowKey="id" size="small" dataSource={list} pagination={{ pageSize: 12 }}
        rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('common.name'), dataIndex: 'name', width: 180, ellipsis: true },
          { title: t('quant.colModel'), dataIndex: 'model_name', width: 200, ellipsis: true },
          { title: t('quant.colMethod'), dataIndex: 'method_label', width: 200, render: (v: string) => <Tag color="geekblue">{v}</Tag> },
          { title: t('common.status'), dataIndex: 'status', width: 100, render: (v: string) => <Tag color={ST[v]}>{v}</Tag> },
          { title: t('common.createTime'), dataIndex: 'created_at', width: 170 },
          { title: t('quant.colFinished'), dataIndex: 'finished_at', width: 170, render: (v: string) => v || '-' },
          {
            title: t('common.actions'), width: 140, render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" onClick={async () => {
                  const d = (await api.get(`/api/quantize/${r.id}`)).data
                  setDetail(d); setLogs('')
                }}>{t('common.detail')}</Button>
                {r.status !== 'pending' && r.status !== 'running' && (
                  <Button size="small" danger onClick={() => Modal.confirm({
                    title: t('common.deleteConfirm'), okText: t('common.delete'), cancelText: t('common.cancel'),
                    onOk: async () => { await api.delete(`/api/quantize/${r.id}`); message.success(t('common.deleted')); load() },
                  })}>{t('common.delete')}</Button>
                )}
              </Space>
            ),
          },
        ]} />

      {/* 新建压缩任务（基本信息 / 量化配置） */}
      <Modal title={t('quant.newTask')} open={open} onCancel={() => setOpen(false)} width={640}
        onOk={async () => {
          const v = await form.validateFields()
          await api.post('/api/quantize', { project_id: projectId, name: v.name, model_asset_id: v.model_asset_id, method: v.method })
          message.success(t('quant.submitted'))
          setOpen(false); load()
        }}>
        <Form form={form} layout="vertical">
          <QuantSection title={t('quant.secBasic')}>
            <Form.Item name="name" label={t('quant.name')} rules={[{ required: true, message: t('quant.select') }]}>
              <Input placeholder={t('quant.namePh')} maxLength={80} showCount />
            </Form.Item>
            <Form.Item name="model_asset_id" label={t('quant.model')} rules={[{ required: true, message: t('quant.select') }]}>
              <Select style={{ width: '100%' }} placeholder={t('quant.modelPh')}
                options={models.map((m: any) => ({ value: m.id, label: `${m.name} (${m.type})` }))} />
            </Form.Item>
          </QuantSection>
          <QuantSection title={t('quant.secMethod')}>
            <Form.Item name="method" label={t('quant.method')} rules={[{ required: true, message: t('quant.select') }]}
              extra={<span style={{ color: '#888' }}>{t('quant.methodHint')}</span>}>
              <Radio.Group value={form.getFieldValue('method')}
                onChange={(e) => form.setFieldsValue({ method: e.target.value })}>
                <Space direction="vertical">
                  {methods.map((m: any) => (
                    <Radio key={m.key} value={m.key}>{m.label}</Radio>
                  ))}
                </Space>
              </Radio.Group>
            </Form.Item>
          </QuantSection>
        </Form>
      </Modal>

      <Drawer title={detail ? t('quant.detailTitleWith', { id: detail.id, name: detail.name }) : t('quant.detailTitle')}
        width={680} open={!!detail} onClose={() => { setDetail(null); setLogs('') }}>
        {detail && (
          <div>
            <Descriptions size="small" column={2} bordered
              items={[
                { key: 'name', label: t('common.name'), children: detail.name },
                { key: 'model', label: t('quant.colModel'), children: detail.model_name || '-' },
                { key: 'method', label: t('quant.colMethod'), children: <Tag color="geekblue">{detail.method_label}</Tag> },
                { key: 'status', label: t('common.status'), children: <Tag color={ST[detail.status]}>{detail.status}</Tag> },
                { key: 'gpu', label: t('quant.gpuCount'), children: detail.gpu_count },
                { key: 'created', label: t('common.createTime'), children: detail.created_at },
                { key: 'finished', label: t('quant.colFinished'), children: detail.finished_at || '-', span: 2 },
              ]} />
            {detail.error && (
              <div style={{ marginTop: 12, padding: 10, background: '#fff2f0', border: '1px solid #ffccc7', borderRadius: 6 }}>
                <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12, color: '#cf1322' }}>{detail.error}</pre>
              </div>
            )}
            <h4 style={{ marginTop: 16, marginBottom: 6 }}>{t('quant.logs')}</h4>
            <pre style={{ background: '#0b1021', color: '#d6e2ff', padding: 12, borderRadius: 6,
              maxHeight: 320, overflow: 'auto', fontSize: 12, whiteSpace: 'pre-wrap' }}>
              {logs || t('quant.logsEmpty')}
            </pre>
          </div>
        )}
      </Drawer>
    </div>
  )
}

// 分区标题（蓝色竖条 + 加粗标题）
function QuantSection({ title, children }: { title: string, children: React.ReactNode }) {
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
