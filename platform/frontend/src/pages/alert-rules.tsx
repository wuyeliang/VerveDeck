// 告警规则页（集群运维 > 告警规则）：通知通道配置 + 阈值/事件规则管理（服务端分页，2000+ 规则）
import React, { useEffect, useState } from 'react'
import {
  Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, message, Popconfirm, Card,
  Switch, Row, Col, AutoComplete, Typography,
} from 'antd'
const { Text } = Typography
import { PlusOutlined, SendOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'
import { METRICS, EVENTS, RULE_PAGE, mLabel, eLabel, sevTag } from './alerts-shared'

/** 通知通道配置：企业微信群机器人 + SMTP 邮件（DB 持久化，env 兜底） */
function ChannelConfigCard() {
  const { t } = useTranslation()
  const [cfg, setCfg] = useState<any>(null)
  const [form] = Form.useForm()
  const [testing, setTesting] = useState('')
  const [saving, setSaving] = useState(false)
  const load = () => api.get('/api/admin/notify-config').then((r) => {
    setCfg(r.data)
    form.setFieldsValue({
      wecom_enabled: r.data.wecom_enabled, wecom_webhook: r.data.wecom_webhook,
      smtp_enabled: r.data.smtp_enabled, smtp_host: r.data.smtp_host,
      smtp_port: r.data.smtp_port, smtp_ssl: r.data.smtp_ssl, smtp_user: r.data.smtp_user,
      smtp_password: r.data.smtp_password_set ? '******' : '',
      smtp_from: r.data.smtp_from, smtp_to: r.data.smtp_to,
    })
  }).catch(() => {})
  useEffect(() => { load() }, [])
  const save = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      await api.put('/api/admin/notify-config', v)
      message.success(t('admin.saved'))
      load()
    } finally { setSaving(false) }
  }
  const test = async (ch: string) => {
    setTesting(ch)
    const res = await api.post('/api/admin/notify-config/test', { channel: ch }).catch(() => null)
    setTesting('')
    if (!res) { message.error(t('admin.testFail')); return }
    if (res.data.ok) message.success(t('admin.testSent'))
    else message.error(res.data.error || t('admin.testFail'))
  }
  const srcTag = (s: string) => s === 'db'
    ? <Tag color="green">{t('admin.srcDb')}</Tag>
    : s === 'env' ? <Tag color="blue">{t('admin.srcEnv')}</Tag>
      : <Tag>{t('admin.srcNone')}</Tag>
  const wecomOk = form.getFieldValue('wecom_enabled') && form.getFieldValue('wecom_webhook')
  const smtpOk = form.getFieldValue('smtp_enabled') && form.getFieldValue('smtp_user')
  return (
    <Card size="small" style={{ marginBottom: 16 }}
      title={<Space>{t('admin.chTitle')}
        <span style={{ color: '#999', fontSize: 12 }}>{t('admin.chHint')}</span></Space>}
      extra={<Button type="primary" size="small" loading={saving} onClick={save}>{t('admin.saveCfg')}</Button>}>
      <Row gutter={[12, 10]}>
        <Col span={6}>
          <Space>
            <Form.Item name="wecom_enabled" valuePropName="checked" noStyle><Switch size="small" /></Form.Item>
            {t('admin.chWecom')} {cfg && srcTag(cfg.wecom_source)}
          </Space>
        </Col>
        <Col span={12}>
          <Form.Item name="wecom_webhook" noStyle>
            <Input placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." size="small" allowClear />
          </Form.Item>
        </Col>
        <Col span={6} style={{ textAlign: 'right' }}>
          <Button size="small" icon={<SendOutlined />} disabled={!wecomOk}
            loading={testing === 'wecom'} onClick={() => test('wecom')}>{t('admin.chTest')}</Button>
        </Col>
        <Col span={6}>
          <Space>
            <Form.Item name="smtp_enabled" valuePropName="checked" noStyle><Switch size="small" /></Form.Item>
            {t('admin.chEmail')} {cfg && srcTag(cfg.smtp_source)}
          </Space>
        </Col>
        <Col span={18} />
        <Col span={6} style={{ textAlign: 'right' }}>
          <Button size="small" icon={<SendOutlined />} disabled={!smtpOk}
            loading={testing === 'email'} onClick={() => test('email')}>{t('admin.chTest')}</Button>
        </Col>
        <Col span={24}><Form.Item noStyle><DividerStyle /></Form.Item></Col>
        <Col span={5}><Form.Item name="smtp_host" noStyle><Input size="small" placeholder={t('admin.smtpHostPh')} allowClear /></Form.Item></Col>
        <Col span={3}><Form.Item name="smtp_port" noStyle><InputNumber size="small" style={{ width: '100%' }} min={1} max={65535} /></Form.Item></Col>
        <Col span={4}>
          <Space size={4}>
            <span style={{ fontSize: 12, color: '#666' }}>{t('admin.smtpSsl')}</span>
            <Form.Item name="smtp_ssl" valuePropName="checked" noStyle><Switch size="small" /></Form.Item>
          </Space>
        </Col>
        <Col span={5}><Form.Item name="smtp_user" noStyle><Input size="small" placeholder={t('admin.smtpUserPh')} allowClear /></Form.Item></Col>
        <Col span={5}><Form.Item name="smtp_password" noStyle><Input.Password size="small" placeholder={t('admin.smtpPwPh')} /></Form.Item></Col>
        <Col span={5}><Form.Item name="smtp_from" noStyle><Input size="small" placeholder={t('admin.smtpFromPh')} allowClear /></Form.Item></Col>
        <Col span={5}><Form.Item name="smtp_to" noStyle><Input size="small" placeholder={t('admin.smtpToPh')} allowClear /></Form.Item></Col>
      </Row>
      <div style={{ marginTop: 8, color: '#999', fontSize: 12 }}>{t('admin.promHint')}</div>
    </Card>
  )
}

const DividerStyle = () => <div style={{ borderTop: '1px dashed #d9d9d9', margin: '2px 0' }} />

/** 告警规则：阈值型（Prometheus 评估）+ 事件型（平台故障收敛点）；服务端分页 + 分类/筛选 + CSV 批量导入 */
export default function AlertRulesPage() {
  const { t } = useTranslation()
  const [rules, setRules] = useState<any[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [sel, setSel] = useState<number[]>([])
  const [cats, setCats] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [editId, setEditId] = useState<number | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [importText, setImportText] = useState('')
  const [importing, setImporting] = useState(false)
  const [form] = Form.useForm()
  const kind = Form.useWatch('kind', form)
  // 规则筛选（服务端）
  const [fCat, setFCat] = useState('')
  const [fKind, setFKind] = useState('')
  const [fEnabled, setFEnabled] = useState('')
  const [fQ, setFQ] = useState('')

  const fetchRules = (p = page, c = fCat, k = fKind, en = fEnabled, q = fQ) =>
    api.get('/api/admin/alerts/rules', {
      params: { limit: RULE_PAGE, offset: (p - 1) * RULE_PAGE,
        category: c || undefined, kind: k || undefined, enabled: en || undefined, q: q || undefined },
    }).then((r) => { setRules(r.data.items || []); setTotal(r.data.total || 0) }).catch(() => {})

  const fetchCats = () =>
    api.get('/api/admin/alerts/rules/categories').then((r) => setCats(r.data.categories || [])).catch(() => {})

  // 筛选/翻页变化即重取；10s 轮询经 ref 取最新参数（当前页 + 当前筛选）
  useEffect(() => { fetchRules() }, [page, fCat, fKind, fEnabled, fQ])
  const pollRef = React.useRef<any>({})
  pollRef.current = { fetchRules, fetchCats }
  useEffect(() => {
    fetchCats()
    const x = setInterval(() => {
      pollRef.current.fetchRules(); pollRef.current.fetchCats()
    }, 10000)
    return () => clearInterval(x)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const batchDel = useBatchDelete('/api/admin/alerts/rules/batch-delete', () => { setSel([]); fetchRules(); fetchCats() })

  const openModal = (r?: any) => {
    form.resetFields()
    if (r) { setEditId(r.id); form.setFieldsValue(r) }
    else { setEditId(null); form.setFieldsValue({ kind: 'threshold', op: '>', threshold: 0, cooldown: 300, severity: 'warning', channels: [], enabled: true, category: '' }) }
    setOpen(true)
  }

  const submit = async () => {
    const v = await form.validateFields()
    if (editId) await api.put(`/api/admin/alerts/rules/${editId}`, v)
    else await api.post('/api/admin/alerts/rules', v)
    message.success(t('admin.created')); setOpen(false); fetchRules(); fetchCats()
  }

  // 批量导入：CSV 行 → 分批（每批 ≤1000）POST /alerts/rules/batch
  const doImport = async () => {
    const lines = importText.split('\n').map((l) => l.trim()).filter(Boolean)
    if (!lines.length) { message.warning(t('admin.bulkImportEmpty')); return }
    const arr: any[] = []
    const errs: string[] = []
    lines.forEach((line, i) => {
      const cols = line.split(',').map((c) => c.trim())
      const [name, category, kindVal, mOrE, op, thr, sev, cd] = cols
      if (!name) { errs.push(`${i + 1}:${t('admin.bulkImportNoName')}`); return }
      if (kindVal === 'event') {
        arr.push({ name, category, kind: 'event', event: mOrE || '', severity: sev || 'warning', cooldown: Number(cd) || 300, enabled: true })
      } else {
        if (!mOrE || !op) { errs.push(`${i + 1}:${t('admin.bulkImportBadRow')}`); return }
        arr.push({ name, category, kind: 'threshold', metric: mOrE, op, threshold: Number(thr) || 0,
          severity: sev || 'warning', cooldown: Number(cd) || 300, enabled: true })
      }
    })
    if (!arr.length) { message.warning(errs.join('; ') || t('admin.bulkImportEmpty')); return }
    setImporting(true)
    try {
      let ok = 0, fail = 0
      for (let i = 0; i < arr.length; i += 1000) {
        const r = await api.post('/api/admin/alerts/rules/batch', { rules: arr.slice(i, i + 1000) })
        ok += (r.data.created || []).length
        fail += (r.data.failed || []).length
      }
      message.success(`${t('admin.bulkImportDone')}：${ok}/${arr.length}`)
      if (fail) message.warning(`${t('admin.bulkImportDone')}：${t('admin.bulkImportFail')} ${fail}`)
      if (errs.length) message.warning(errs.join('; '))
      setImportOpen(false); setImportText('')
      setPage(1); fetchRules(); fetchCats()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || e?.message)
    } finally { setImporting(false) }
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('admin.tabAlertRules')}</h3>
      <ChannelConfigCard />
      <Card size="small" title={<Space size={8}>{t('admin.alertRules')}<Text type="secondary" style={{ fontSize: 12 }}>{t('admin.ruleTotal')} {total}</Text></Space>}
        extra={<Space size={8}>
          {sel.length > 0 && <Button size="small" danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button size="small" onClick={() => { setImportText(''); setImportOpen(true) }}>{t('admin.bulkImport')}</Button>
          <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => openModal()}>{t('admin.newRule')}</Button>
        </Space>}>
        <Space size={6} style={{ marginBottom: 8 }} wrap>
          <Select size="small" allowClear placeholder={t('admin.ruleCategoryAll')} style={{ width: 140 }}
            value={fCat || undefined} onChange={(v) => { setFCat(v || ''); setPage(1) }}
            options={cats.map((c) => ({ value: c, label: c }))} />
          <Select size="small" allowClear placeholder={t('admin.colKind')} style={{ width: 110 }}
            value={fKind || undefined} onChange={(v) => { setFKind(v || ''); setPage(1) }}
            options={[{ value: 'threshold', label: t('admin.kindThreshold') }, { value: 'event', label: t('admin.kindEvent') }]} />
          <Select size="small" allowClear placeholder={t('common.status')} style={{ width: 110 }}
            value={fEnabled || undefined} onChange={(v) => { setFEnabled(v || ''); setPage(1) }}
            options={[{ value: '1', label: t('common.enabled') }, { value: '0', label: t('common.disabled') }]} />
          <Input size="small" allowClear placeholder={t('admin.ruleName')} style={{ width: 180 }}
            value={fQ} onChange={(e) => { setFQ(e.target.value); setPage(1) }} />
        </Space>
        <Table size="small" rowKey="id" dataSource={rules}
          pagination={{ current: page, pageSize: RULE_PAGE, total, showSizeChanger: false, onChange: (p: number) => setPage(p) }}
          rowSelection={{ selectedRowKeys: sel, preserveSelectedRowKeys: true, onChange: (k: any) => setSel(k) }}
          columns={[
            { title: t('admin.ruleName'), dataIndex: 'name', width: 180, ellipsis: true },
            { title: t('admin.colCategory'), dataIndex: 'category', width: 100, render: (v: string) => v ? <Tag color="purple">{v}</Tag> : <Text type="secondary">-</Text> },
            { title: t('admin.colKind'), dataIndex: 'kind', width: 80, render: (v: string) => v === 'event' ? <Tag>{t('admin.kindEvent')}</Tag> : <Tag color="blue">{t('admin.kindThreshold')}</Tag> },
            {
              title: t('admin.colMetric'), width: 170, render: (_: any, r: any) =>
                r.kind === 'event' ? eLabel(t, r.event) : mLabel(t, r.metric),
            },
            { title: t('admin.colCond'), width: 130, render: (_: any, r: any) => r.kind === 'threshold' ? `${r.op} ${r.threshold}` : '-' },
            { title: t('admin.colSeverity'), dataIndex: 'severity', width: 90, render: (v: string) => sevTag(t, v) },
            { title: t('admin.colCooldown'), dataIndex: 'cooldown', width: 90, render: (v: number) => `${v}s` },
            {
              title: t('admin.colChannels'), dataIndex: 'channels', width: 150, render: (c: string[]) => (!c || !c.length)
                ? <Tag>{t('admin.chAuto')}</Tag>
                : <>{c.includes('wecom') && <Tag color="green">{t('admin.chWecom')}</Tag>}{c.includes('email') && <Tag color="blue">{t('admin.chEmail')}</Tag>}</>,
            },
            { title: t('common.status'), dataIndex: 'enabled', width: 80, render: (v: boolean) => v ? <Tag color="success">{t('common.enabled')}</Tag> : <Tag>{t('common.disabled')}</Tag> },
            {
              title: t('common.actions'), width: 170, render: (_: any, r: any) => (
                <Space size={4}>
                  <Button size="small" onClick={() => openModal(r)}>{t('admin.editRule')}</Button>
                  <Button size="small" onClick={async () => {
                    const res = await api.post(`/api/admin/alerts/rules/${r.id}/test`)
                    if (res.data.ok) message.success(t('admin.testSent'))
                    else message.error(res.data.error || t('admin.testFail'))
                  }}>{t('admin.testNotify')}</Button>
                  <Popconfirm title={t('admin.del')} onConfirm={() => api.delete(`/api/admin/alerts/rules/${r.id}`).then(() => { fetchRules(); fetchCats() })}>
                    <Button size="small" danger>{t('common.delete')}</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]} />
      </Card>
      <Modal title={editId ? t('admin.editRule') : t('admin.newRule')} open={open} onCancel={() => setOpen(false)} onOk={submit} width={520}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('admin.ruleName')} rules={[{ required: true }]}><Input maxLength={128} /></Form.Item>
          <Form.Item name="category" label={t('admin.ruleCategory')} extra={t('admin.ruleCategoryPh')}>
            <AutoComplete maxLength={64} options={cats.map((c) => ({ value: c }))} placeholder={t('admin.ruleCategoryPh')} />
          </Form.Item>
          <Form.Item name="kind" label={t('admin.colKind')}>
            <Select options={[{ value: 'threshold', label: t('admin.kindThreshold') }, { value: 'event', label: t('admin.kindEvent') }]} />
          </Form.Item>
          {kind !== 'event' && (
            <Space.Compact block style={{ display: 'flex', gap: 8 }}>
              <Form.Item name="metric" style={{ flex: 2 }} rules={[{ required: true }]}>
                <Select options={METRICS.map((m) => ({ value: m, label: mLabel(t, m) }))} />
              </Form.Item>
              <Form.Item name="op" style={{ flex: 1 }} rules={[{ required: true }]}>
                <Select options={[{ value: '>', label: '>' }, { value: '>=', label: '>=' }, { value: '<', label: '<' }, { value: '<=', label: '<=' }]} />
              </Form.Item>
              <Form.Item name="threshold" style={{ flex: 1 }} rules={[{ required: true }]}>
                <InputNumber style={{ width: '100%' }} min={0} step={1} />
              </Form.Item>
            </Space.Compact>
          )}
          {kind === 'event' && (
            <Form.Item name="event" label={t('admin.colEvent')} rules={[{ required: true }]}>
              <Select options={EVENTS.map((e) => ({ value: e, label: eLabel(t, e) }))} />
            </Form.Item>
          )}
          <Space.Compact block style={{ display: 'flex', gap: 8 }}>
            <Form.Item name="severity" style={{ flex: 1 }}>
              <Select options={[{ value: 'warning', label: t('admin.sevWarning') }, { value: 'critical', label: t('admin.sevCritical') }]} />
            </Form.Item>
            <Form.Item name="cooldown" style={{ flex: 1 }}><InputNumber style={{ width: '100%' }} min={0} step={30} addonAfter="s" /></Form.Item>
          </Space.Compact>
          <Form.Item name="channels" label={t('admin.colChannels')} extra={t('admin.chAuto')}>
            <Select mode="multiple" allowClear options={[{ value: 'wecom', label: t('admin.chWecom') }, { value: 'email', label: t('admin.chEmail') }]} />
          </Form.Item>
          <Form.Item name="enabled">
            <Select options={[{ value: true, label: t('common.enabled') }, { value: false, label: t('common.disabled') }]} />
          </Form.Item>
        </Form>
      </Modal>
      <Modal title={t('admin.bulkImportTitle')} open={importOpen} onCancel={() => setImportOpen(false)}
        onOk={doImport} okButtonProps={{ loading: importing }} okText={t('admin.bulkImportBtn')} width={640}>
        <div style={{ fontSize: 12, color: '#888', marginBottom: 8, whiteSpace: 'pre-wrap' }}>{t('admin.bulkImportFmt')}</div>
        <Input.TextArea rows={10} value={importText} onChange={(e) => setImportText(e.target.value)}
          placeholder={'GPU 显存过高,GPU,threshold,gpu_mem,>,95,warning,300\n训练任务失败,task,event,train_failed,,,warning,60'} />
      </Modal>
    </div>
  )
}
