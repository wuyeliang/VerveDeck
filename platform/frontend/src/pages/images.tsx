import React, { useEffect, useState } from 'react'
import { Card, Table, Tag, Button, Space, Modal, Form, Input, Popconfirm, Tooltip, message } from 'antd'
import { useBatchDelete } from '../batch'
import { ReloadOutlined, PlusOutlined, DeleteOutlined, DownloadOutlined, BuildOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

function fmtBytes(n: number): string {
  if (!n) return '-'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  n = Number(n) || 0
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`
}

const SOURCE_COLOR: Record<string, string> = { official: 'green', selfbuilt: 'blue', custom: 'purple' }

/** 镜像管理（一级菜单 /images）：平台引擎镜像版本/拉取/重建 + 自定义镜像（国产芯片等） */
export default function ImagesPage() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [sel, setSel] = useState<string[]>([])
  const [busy, setBusy] = useState('')
  const [addOpen, setAddOpen] = useState(false)
  const [form] = Form.useForm()

  const load = () => api.get('/api/images').then((r) => setList(r.data || [])).catch(() => {})
  const batchDel = useBatchDelete('/api/images/custom/batch-delete', () => { setSel([]); load() })
  const pulling = list.some((r) => r.pull_status?.status === 'running')
  // 有后台拉取/构建时 5s 轮询，否则 30s
  useEffect(() => { load(); const x = setInterval(load, pulling ? 5000 : 30000); return () => clearInterval(x) }, [pulling])

  const act = async (key: string, kind: 'pull' | 'build' | 'probe', label: string) => {
    setBusy(`${key}-${kind}`)
    try {
      const url = kind === 'probe' ? `/api/images/${key}/probe`
        : `/api/images/${key}/refresh${kind === 'build' ? '?force=true' : ''}`
      await api.post(url)
      message.success(label)
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    } finally {
      setBusy('')
    }
  }

  const addCustom = async () => {
    try {
      const v = await form.validateFields()
      await api.post('/api/images/custom', v)
      message.success(t('common.created'))
      setAddOpen(false); form.resetFields(); load()
    } catch (e: any) {
      if (e?.errorFields) return
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    }
  }
  const delCustom = async (id: number) => {
    try {
      await api.delete(`/api/images/custom/${id}`)
      message.success(t('common.deleted')); load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    }
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('nav.images')}</h3>
      <Card size="small" title={t('images.title')}
        extra={<Space size={8}>
          {sel.length > 0 && <Button size="small" danger onClick={() => batchDel(sel.map((k) => Number(k.split('-').pop())))}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button size="small" icon={<ReloadOutlined />} onClick={load}>{t('common.refresh')}</Button>
        </Space>}>
        <Table size="small" rowKey="key" dataSource={list} pagination={false}
          rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k), getCheckboxProps: (r: any) => ({ disabled: r.source !== 'custom' }) }}
          columns={[
            { title: t('images.colComponent'), dataIndex: 'component', width: 190,
              render: (v: string, r: any) => {
                // 组件名/说明走前端 i18n（后端返回的中文仅兜底），自定义行用 custom 文案
                const comp = r.source === 'custom' ? t('images.customImage') : t(`images.comp.${r.key}`, { defaultValue: v })
                const desc = r.source === 'custom' ? (r.desc || '') : t(`images.desc.${r.key}`, { defaultValue: r.desc })
                return <div><div style={{ fontWeight: 600 }}>{comp}</div><div style={{ fontSize: 11, color: '#999' }}>{desc}</div></div>
              } },
            { title: t('images.colImage'), dataIndex: 'image',
              render: (v: string) => <code style={{ fontSize: 11 }}>{v}</code> },
            {
              title: t('images.colSource'), dataIndex: 'source', width: 90,
              render: (v: string) => <Tag color={SOURCE_COLOR[v] || 'default'}>{t(`images.src${v[0].toUpperCase()}${v.slice(1)}`, { defaultValue: v })}</Tag>,
            },
            {
              title: t('images.colVersion'), dataIndex: 'version', width: 220,
              render: (v: string) => <Tooltip title={v}><code style={{ fontSize: 11 }}>{(v || '-').slice(0, 38)}{(v || '').length > 38 ? '…' : ''}</code></Tooltip>,
            },
            { title: t('images.colSize'), dataIndex: 'size', width: 90, render: (v: number) => fmtBytes(v) },
            {
              title: t('images.colStatus'), width: 110,
              render: (_: any, r: any) => {
                const ps = r.pull_status
                if (ps?.status === 'running')
                  return <Tag color="processing">{ps.kind === 'build' ? t('images.statusBuilding') : t('images.statusPulling')}</Tag>
                if (ps?.status === 'failed')
                  return <Tooltip title={ps.error}><Tag color="error">{t('images.statusFailed')}</Tag></Tooltip>
                return r.exists ? <Tag color="success">{t('images.present')}</Tag>
                  : <Tag>{t('images.notDownloaded')}</Tag>
              },
            },
            {
              title: t('common.actions'), width: 170,
              render: (_: any, r: any) => (
                <Space size={4}>
                  {r.source === 'official'
                    ? <Button size="small" icon={<DownloadOutlined />} loading={busy === `${r.key}-pull`}
                      onClick={() => act(r.key, 'pull', t('images.started'))}>{t('images.pull')}</Button>
                    : r.source === 'selfbuilt'
                      ? <Button size="small" icon={<BuildOutlined />} loading={busy === `${r.key}-build`}
                        onClick={() => act(r.key, 'build', t('images.started'))}>{t('images.rebuild')}</Button>
                      : <Popconfirm title={t('images.delCustomConfirm')}
                        onConfirm={async () => {
                          try { await api.delete(`/api/images/custom/${r.key.split('-').pop()}`); message.success(t('common.deleted')); load() }
                          catch (e: any) { message.error(e.response?.data?.detail || t('api.requestFailed')) }
                        }}>
                        <Button size="small" danger icon={<DeleteOutlined />} />
                      </Popconfirm>}
                  <Button size="small" icon={<ReloadOutlined />} loading={busy === `${r.key}-probe`}
                    onClick={() => act(r.key, 'probe', t('images.probed'))}>{t('images.probe')}</Button>
                </Space>
              ),
            },
          ]} />
      </Card>
      <Card size="small" title={t('images.customTitle')} style={{ marginTop: 16 }}
        extra={<Button type="primary" size="small" icon={<PlusOutlined />}
          onClick={() => { form.resetFields(); setAddOpen(true) }}>{t('images.addCustom')}</Button>}>
        <p style={{ color: '#999', fontSize: 12, margin: 0 }}>
          {t('images.customHint')}
        </p>
      </Card>
      <Modal title={t('images.addCustom')} open={addOpen} onCancel={() => setAddOpen(false)} onOk={addCustom}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('images.colImage')} rules={[{ required: true }]}
            extra={t('images.nameExtra')}>
            <Input placeholder="vllm/vllm-ascend:0.33.0-npu" />
          </Form.Item>
          <Form.Item name="note" label={t('common.desc')}><Input placeholder={t('images.notePh')} /></Form.Item>
        </Form>
      </Modal>
    </div>
  )
}
