// 集群升级页（集群运维 > 集群升级）：官方镜像换 tag（pull + 运行时 pin）/ agent 蓝绿重建 / backend·web 自构重建
// 自「系统管理」迁入，API 不变（/api/admin/upgrade/*，仍仅超管可调，API 侧有角色校验）
import React, { useEffect, useState } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Select, message,
} from 'antd'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

export default function ClusterUpgradePage() {
  const { t } = useTranslation()
  const [rows, setRows] = useState<any[]>([])
  const [tasks, setTasks] = useState<any[]>([])
  const [upTarget, setUpTarget] = useState<any>(null)
  const [versions, setVersions] = useState<string[]>([])
  const [ver, setVer] = useState('')
  const [checking, setChecking] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [loading, setLoading] = useState(false)

  const load = () => {
    setLoading(true)
    api.get('/api/admin/upgrade/status').then((r) => setRows(r.data)).catch(() => {})
    api.get('/api/admin/upgrade/tasks', { params: { limit: 50 } }).then((r) => setTasks(r.data)).catch(() => {})
    setLoading(false)
  }
  useEffect(() => { load(); const x = setInterval(load, 5000); return () => clearInterval(x) }, [])

  const openUpgrade = async (row: any) => {
    setUpTarget(row)
    setVer('')
    setVersions([])
    if (row.kind === 'pull') {
      setChecking(true)
      const r = await api.post('/api/admin/upgrade/check', { target: row.key }).catch(() => null)
      setChecking(false)
      if (r) setVersions(r.data.latest || [])
    }
  }

  const submit = async () => {
    if (upTarget?.kind === 'pull' && !ver) { message.warning(t('admin.upNeedVer')); return }
    setSubmitting(true)
    try {
      await api.post('/api/admin/upgrade/execute', { target: upTarget.key, to_version: ver })
      message.success(t('admin.upStarted'))
      setUpTarget(null)
      load()
    } catch { /* 拦截器已提示错误 */ }
    finally { setSubmitting(false) }
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('admin.tabUpgrade')}</h3>
      <Table size="small" rowKey="key" dataSource={rows} pagination={false} loading={loading}
        columns={[
          { title: t('common.name'), dataIndex: 'label', width: 190 },
          {
            title: t('admin.upImage'), width: 300, render: (_: any, r: any) => (
              <Space direction="vertical" size={0}>
                <code style={{ fontSize: 12 }}>{r.image}</code>
                <span style={{ fontSize: 11, color: '#999' }}>{r.runtime}</span>
              </Space>
            ),
          },
          {
            title: t('admin.upLocal'), width: 110, render: (_: any, r: any) => r.exists
              ? <Tag color="success">{t('admin.upPresent')} {((r.size / 1024 / 1024 / 1024) || 0).toFixed(1)} GB</Tag>
              : <Tag>{t('admin.upMissing')}</Tag>,
          },
          {
            title: t('admin.upImpact'), render: (_: any, r: any) => (
              <span style={{ fontSize: 12, color: '#888' }}>{r.impact_hint}</span>
            ),
          },
          {
            title: t('common.status'), width: 110, render: (_: any, r: any) => r.running_task
              ? <Tag color="processing">{t('admin.upRunning')}</Tag>
              : <Tag>{t('admin.upIdle')}</Tag>,
          },
          {
            title: t('common.actions'), width: 100, render: (_: any, r: any) => (
              <Button size="small" type="primary" disabled={!!r.running_task} onClick={() => openUpgrade(r)}>
                {r.kind === 'pull' ? t('admin.upUpgrade') : t('admin.upRebuild')}
              </Button>
            ),
          },
        ]} />
      <h4 style={{ margin: '16px 0 8px' }}>{t('admin.upTasks')}</h4>
      <Table size="small" rowKey="id" dataSource={tasks} pagination={{ pageSize: 10, size: 'small' }}
        expandable={{
          expandedRowRender: (r: any) => (
            <pre style={{ maxHeight: 300, overflow: 'auto', fontSize: 12, background: 'rgba(0,0,0,0.03)', padding: 8, margin: 0 }}>
              {r.log || '-'}
            </pre>
          ),
          rowExpandable: (r: any) => !!r.log,
        }}
        columns={[
          { title: t('common.id'), dataIndex: 'id', width: 55 },
          { title: t('admin.upTarget'), dataIndex: 'target', width: 110 },
          {
            title: t('admin.upVersion'), render: (_: any, r: any) => (
              <span style={{ fontSize: 12 }}>{r.from_version} → <b>{r.to_version}</b></span>
            ),
          },
          {
            title: t('common.status'), dataIndex: 'status', width: 95,
            render: (v: string) => v === 'running'
              ? <Tag color="processing">{t('admin.upRunning')}</Tag>
              : v === 'success' ? <Tag color="success">{t('admin.upSuccess')}</Tag>
                : <Tag color="error">{t('admin.upFailed')}</Tag>,
          },
          { title: t('admin.colTime'), dataIndex: 'created_at', width: 170, render: (v: string) => v.replace('T', ' ').slice(0, 19) },
        ]} />
      <Modal title={upTarget ? `${upTarget.label} · ${upTarget.kind === 'pull' ? t('admin.upUpgrade') : t('admin.upRebuild')}` : ''}
        open={!!upTarget} onCancel={() => setUpTarget(null)}
        onOk={submit} confirmLoading={submitting}
        okButtonProps={{ disabled: upTarget?.kind === 'pull' && (!ver || checking) }}>
        {upTarget && (
          <div>
            <AlertLine>{upTarget.impact_hint}</AlertLine>
            {upTarget.kind === 'pull' ? (
              <Form layout="vertical" style={{ marginTop: 10 }}>
                <Form.Item label={t('admin.upToVer')} required>
                  <Select value={ver || undefined} onChange={setVer} loading={checking} showSearch allowClear
                    placeholder={t('admin.upPickVer')}
                    options={versions.map((v) => ({ value: v, label: v }))} />
                </Form.Item>
                <div style={{ fontSize: 12, color: '#999' }}>{t('admin.upCurrentImage')}: <code>{upTarget.image}</code></div>
              </Form>
            ) : (
              <div style={{ fontSize: 13, color: '#666' }}>{t('admin.upRebuildHint')}</div>
            )}
          </div>
        )}
      </Modal>
    </div>
  )
}

const AlertLine = ({ children }: { children: React.ReactNode }) => (
  <div style={{ background: 'rgba(250,173,20,0.12)', border: '1px solid rgba(250,173,20,0.45)',
    borderRadius: 6, padding: '6px 10px', fontSize: 12, color: '#874d00' }}>{children}</div>
)
