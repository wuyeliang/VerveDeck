import React, { useEffect, useState } from 'react'
import {
  Tabs, Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, message, Popconfirm, Card,
  Switch, Row, Col, Tooltip,
} from 'antd'
import { PlusOutlined, CopyOutlined, SendOutlined, ClockCircleOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useBatchDelete } from '../batch'
import { useTheme } from '../theme'
import { LANGS, setLang } from '../i18n'

export default function AdminPanel() {
  const { t } = useTranslation()
  const [tab, setTab] = useState('keys')
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('admin.title')}</h3>
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'keys', label: t('admin.tabKeys'), children: <KeysTab /> },
        { key: 'guard', label: t('admin.tabGuard'), children: <GuardTab /> },
        { key: 'upgrade', label: t('admin.tabUpgrade'), children: <UpgradeTab /> },
        { key: 'settings', label: t('admin.tabSettings'), children: <SettingsTab /> },
      ]} />
    </div>
  )
}

/** 告警中心（独立页，挂在集群运维菜单下）：通知通道 + 阈值规则 + 事件规则 + 告警事件 */
export function AlertsPage() {
  const { t } = useTranslation()
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>{t('admin.tabAlerts')}</h3>
      <ChannelConfigCard />
      <AlertsTab />
    </div>
  )
}

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

function KeysTab() {
  const { t } = useTranslation()
  const [projects, setProjects] = useState<any[]>([])
  const [keys, setKeys] = useState<any[]>([])
  const [models, setModels] = useState<any[]>([])
  const [pid, setPid] = useState<number>(0)
  const [sel, setSel] = useState<number[]>([])
  const [newKey, setNewKey] = useState('')
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const load = () => {
    api.get('/api/admin/projects').then((r) => {
      setProjects(r.data)
      if (r.data.length && !r.data.find((p) => p.id === pid)) setPid(r.data[0].id)
    }).catch(() => {})
  }
  const loadKeys = () => {
    if (!pid) return
    api.get(`/api/admin/projects/${pid}/api-keys`).then((r) => setKeys(r.data)).catch(() => {})
    api.get('/api/models', { params: { project_id: pid } }).then((r) => setModels(r.data || [])).catch(() => {})
  }
  useEffect(() => { load() }, [])
  useEffect(() => { loadKeys() }, [pid])
  const batchDel = useBatchDelete(`/api/admin/projects/${pid}/api-keys/batch-delete`, () => { setSel([]); loadKeys() })
  return (
    <div>
      <Space style={{ marginBottom: 12 }}>
        <Select value={pid || undefined} style={{ width: 240 }} placeholder={t('admin.selectProject')}
          options={projects.map((p) => ({ value: p.id, label: p.name }))} onChange={setPid} />
        {sel.length > 0 && <Button size="small" danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
        <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setOpen(true) }}>{t('admin.createKey')}</Button>
      </Space>
      {newKey && (
        <Card size="small" style={{ marginBottom: 12, borderColor: '#fa8c16' }}>
          <Space>
            <code style={{ fontSize: 13 }}>{newKey}</code>
            <Button size="small" icon={<CopyOutlined />} onClick={() => navigator.clipboard?.writeText(newKey)}>{t('admin.copy')}</Button>
            <Tag color="orange">{t('admin.showOnce')}</Tag>
          </Space>
        </Card>
      )}
      <Table size="small" rowKey="id" dataSource={keys} pagination={false}
        rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }}
        columns={[
          { title: t('common.name'), dataIndex: 'name' },
          { title: t('admin.colPrefix'), dataIndex: 'key_prefix', render: (v: string) => <code>{v}…</code> },
          {
            title: t('admin.keyModels'), dataIndex: 'model_ids', width: 160,
            render: (ids: number[]) => (!ids || !ids.length)
              ? <Tag>{t('admin.keyScopeAll')}</Tag>
              : <Tag color="blue">{t('admin.keyScopeN', { n: ids.length })}</Tag>,
          },
          { title: t('common.status'), dataIndex: 'is_active', width: 80, render: (v: boolean) => v ? <Tag color="success">{t('common.enabled')}</Tag> : <Tag>{t('common.disabled')}</Tag> },
          {
            title: t('common.actions'), width: 90, render: (_: any, r: any) => (
              <Popconfirm title={t('admin.disableKey')} onConfirm={() => api.delete(`/api/admin/projects/${pid}/api-keys/${r.id}`).then(loadKeys)}>
                <Button size="small" danger>{t('admin.disable')}</Button>
              </Popconfirm>
            ),
          },
        ]} />
      <Modal title={t('admin.createKey')} open={open} onCancel={() => setOpen(false)}
        onOk={async () => {
          const v = await form.validateFields()
          const r = await api.post(`/api/admin/projects/${pid}/api-keys`, v)
          setNewKey(r.data.key); setOpen(false); loadKeys()
        }}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('common.name')} rules={[{ required: true }]}><Input /></Form.Item>
          <Form.Item name="model_ids" label={t('admin.keyModels')} extra={t('admin.keyModelsHint')}>
            <Select mode="multiple" allowClear optionFilterProp="label" placeholder={t('admin.keyScopeAll')}
              options={models.map((m: any) => ({ value: m.id, label: m.name }))} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

function GuardTab() {
  const { t } = useTranslation()
  const [list, setList] = useState<any[]>([])
  const [sel, setSel] = useState<number[]>([])
  const [form] = Form.useForm()
  const load = () => api.get('/api/admin/guard-rules').then((r) => setList(r.data)).catch(() => {})
  useEffect(() => { load() }, [])
  const batchDel = useBatchDelete('/api/admin/guard-rules/batch-delete', () => { setSel([]); load() })
  return (
    <div>
      <Card size="small" title={t('admin.guardTitle')} style={{ marginBottom: 12 }}>
        <Form form={form} layout="inline" onFinish={async (v) => { await api.post('/api/admin/guard-rules', v); message.success(t('admin.created')); form.resetFields(); load() }}>
          <Form.Item name="pattern" rules={[{ required: true }]}><Input placeholder={t('admin.patternPh')} style={{ width: 240 }} /></Form.Item>
          <Form.Item name="scope"><Select style={{ width: 130 }} options={[{ value: 'both', label: t('admin.scopeBoth') }, { value: 'input', label: t('admin.scopeInput') }, { value: 'output', label: t('admin.scopeOutput') }]} /></Form.Item>
          <Form.Item name="description"><Input placeholder={t('admin.descPh')} style={{ width: 200 }} /></Form.Item>
          <Button type="primary" htmlType="submit">{t('admin.add')}</Button>
        </Form>
      </Card>
      {sel.length > 0 && (
        <Button size="small" danger style={{ marginBottom: 8 }} onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>
      )}
      <Table size="small" rowKey="id" dataSource={list} pagination={false}
        rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }}
        columns={[
          { title: t('admin.colPattern'), dataIndex: 'pattern' },
          { title: t('admin.colScope'), dataIndex: 'scope', width: 100 },
          { title: t('common.desc'), dataIndex: 'description', ellipsis: true },
          { title: t('admin.colEnabled'), dataIndex: 'enabled', width: 70, render: (v: boolean) => v ? <Tag color="success">{t('common.yes')}</Tag> : <Tag>{t('common.no')}</Tag> },
          { title: t('common.actions'), width: 80, render: (_: any, r: any) => <Popconfirm title={t('admin.del')} onConfirm={() => api.delete(`/api/admin/guard-rules/${r.id}`).then(load)}><Button size="small" danger>{t('common.delete')}</Button></Popconfirm> },
        ]} />
    </div>
  )
}

/** 告警：阈值规则（宿主/GPU/推理/监控自身，Prometheus 评估）+ 事件规则（平台故障收敛点）+ 告警事件 */
const METRICS = ['host_cpu', 'host_mem', 'host_disk', 'gpu_util', 'gpu_temp', 'gpu_mem',
  'gpu_mem_temp', 'gpu_power', 'gpu_xid', 'gpu_ecc_dbe', 'kv_cache', 'infer_waiting',
  'infer_latency_p95', 'infer_down', 'system_up']
const EVENTS = ['node_offline', 'node_online', 'train_failed', 'eval_failed', 'quant_failed', 'infer_down']

function AlertsTab() {
  const { t, i18n: ti18n } = useTranslation()
  const [rules, setRules] = useState<any[]>([])
  const [sel, setSel] = useState<number[]>([])
  const [events, setEvents] = useState<any[]>([])
  const [evLevel, setEvLevel] = useState('')
  const [evTarget, setEvTarget] = useState('')
  const [open, setOpen] = useState(false)
  const [editId, setEditId] = useState<number | null>(null)
  const [form] = Form.useForm()
  const kind = Form.useWatch('kind', form)
  const mLabel = (m: string) => ({
    host_cpu: t('admin.mHostCpu'), host_mem: t('admin.mHostMem'), host_disk: t('admin.mHostDisk'),
    gpu_util: t('admin.mGpuUtil'), gpu_temp: t('admin.mGpuTemp'), gpu_mem: t('admin.mGpuMem'),
    gpu_mem_temp: t('admin.mGpuMemTemp'), gpu_power: t('admin.mGpuPower'),
    gpu_xid: t('admin.mGpuXid'), gpu_ecc_dbe: t('admin.mGpuEccDbe'),
    kv_cache: t('admin.mKvCache'), infer_waiting: t('admin.mInferWaiting'),
    infer_latency_p95: t('admin.mInferLatency'), infer_down: t('admin.mInferDown'),
    system_up: t('admin.mSystemUp'),
  }[m] || m)
  const eLabel = (e: string) => ({
    node_offline: t('admin.evNodeOffline'), node_online: t('admin.evNodeOnline'),
    train_failed: t('admin.evTrainFailed'), eval_failed: t('admin.evEvalFailed'),
    quant_failed: t('admin.evQuantFailed'), infer_down: t('admin.evInferDown'),
  }[e] || e)

  const load = () => {
    api.get('/api/admin/alerts/rules').then((r) => setRules(r.data)).catch(() => {})
    api.get('/api/admin/alerts/events?limit=100').then((r) => setEvents(r.data.items || [])).catch(() => {})
  }
  useEffect(() => { load(); const x = setInterval(load, 10000); return () => clearInterval(x) }, [])
  const batchDel = useBatchDelete('/api/admin/alerts/rules/batch-delete', () => { setSel([]); load() })

  const openModal = (r?: any) => {
    form.resetFields()
    if (r) { setEditId(r.id); form.setFieldsValue(r) }
    else { setEditId(null); form.setFieldsValue({ kind: 'threshold', op: '>', threshold: 0, cooldown: 300, severity: 'warning', channels: [], enabled: true }) }
    setOpen(true)
  }

  const submit = async () => {
    const v = await form.validateFields()
    if (editId) await api.put(`/api/admin/alerts/rules/${editId}`, v)
    else await api.post('/api/admin/alerts/rules', v)
    message.success(t('admin.created')); setOpen(false); load()
  }

  const sevTag = (v: string) => v === 'critical' ? <Tag color="error">{t('admin.sevCritical')}</Tag> : <Tag color="warning">{t('admin.sevWarning')}</Tag>

  return (
    <div>
      <Card size="small" title={t('admin.alertRules')} style={{ marginBottom: 16 }}
        extra={<Space size={8}>
          {sel.length > 0 && <Button size="small" danger onClick={() => batchDel(sel)}>{t('common.batchDelete')}（{sel.length}）</Button>}
          <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => openModal()}>{t('admin.newRule')}</Button>
        </Space>}>
        <Table size="small" rowKey="id" dataSource={rules} pagination={false}
          rowSelection={{ selectedRowKeys: sel, onChange: (k: any) => setSel(k) }}
          columns={[
            { title: t('admin.ruleName'), dataIndex: 'name', width: 180, ellipsis: true },
            { title: t('admin.colKind'), dataIndex: 'kind', width: 80, render: (v: string) => v === 'event' ? <Tag>{t('admin.kindEvent')}</Tag> : <Tag color="blue">{t('admin.kindThreshold')}</Tag> },
            {
              title: t('admin.colMetric'), width: 170, render: (_: any, r: any) =>
                r.kind === 'event' ? eLabel(r.event) : mLabel(r.metric),
            },
            { title: t('admin.colCond'), width: 130, render: (_: any, r: any) => r.kind === 'threshold' ? `${r.op} ${r.threshold}` : '-' },
            { title: t('admin.colSeverity'), dataIndex: 'severity', width: 90, render: sevTag },
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
                  <Popconfirm title={t('admin.del')} onConfirm={() => api.delete(`/api/admin/alerts/rules/${r.id}`).then(load)}>
                    <Button size="small" danger>{t('common.delete')}</Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]} />
      </Card>
      <Card size="small" title={t('admin.eventsTitle')}
        extra={
          <Space size={6}>
            <Input size="small" allowClear placeholder={t('admin.evSearchPh')} style={{ width: 150 }}
              value={evTarget} onChange={(e) => setEvTarget(e.target.value)} />
            <Select size="small" allowClear placeholder={t('admin.colLevel')} style={{ width: 110 }}
              value={evLevel || undefined} onChange={(v) => setEvLevel(v || '')}
              options={[
                { value: 'critical', label: t('admin.sevCritical') },
                { value: 'warning', label: t('admin.sevWarning') },
                { value: 'info', label: t('admin.sevInfo') },
              ]} />
          </Space>
        }>
        <Table size="small" rowKey="id" dataSource={events.filter((e) =>
          (!evLevel || e.level === evLevel) &&
          (!evTarget || (e.target || '').toLowerCase().includes(evTarget.toLowerCase())))}
          pagination={false} locale={{ emptyText: '-' }}
          columns={[
            { title: t('admin.colTime'), width: 160, render: (_: any, r: any) => new Date(r.ts * 1000).toLocaleString(ti18n.language) },
            { title: t('admin.colLevel'), dataIndex: 'level', width: 90, render: sevTag },
            { title: t('admin.colTarget'), dataIndex: 'target', width: 110 },
            { title: t('admin.colTitle'), dataIndex: 'title', ellipsis: true },
            { title: t('admin.colNotified'), dataIndex: 'notified', width: 90, render: (v: boolean) => v ? <Tag color="success">{t('common.yes')}</Tag> : <Tag>{t('common.no')}</Tag> },
          ]} />
      </Card>
      <Modal title={editId ? t('admin.editRule') : t('admin.newRule')} open={open} onCancel={() => setOpen(false)} onOk={submit} width={520}>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t('admin.ruleName')} rules={[{ required: true }]}><Input maxLength={128} /></Form.Item>
          <Form.Item name="kind" label={t('admin.colKind')}>
            <Select options={[{ value: 'threshold', label: t('admin.kindThreshold') }, { value: 'event', label: t('admin.kindEvent') }]} />
          </Form.Item>
          {kind !== 'event' && (
            <Space.Compact block style={{ display: 'flex', gap: 8 }}>
              <Form.Item name="metric" style={{ flex: 2 }} rules={[{ required: true }]}>
                <Select options={METRICS.map((m) => ({ value: m, label: mLabel(m) }))} />
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
              <Select options={EVENTS.map((e) => ({ value: e, label: eLabel(e) }))} />
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
    </div>
  )
}

/** 集群升级：官方镜像换 tag（pull + 运行时 pin）/ agent 蓝绿重建 / backend·web 自构重建 */
function UpgradeTab() {
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

  const busy = tasks.some((x) => x.status === 'running')

  return (
    <div>
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

/** 平台设置：主题 + 语言（原 Header 右上角入口迁至此） */
function SettingsTab() {
  const { t, i18n: ti18n } = useTranslation()
  const { mode, setMode } = useTheme()
  return (
    <div>
      <Card size="small" title={t('admin.tabSettings')} style={{ maxWidth: 480 }}>
        <Form layout="vertical">
          <Form.Item label={t('header.theme')}>
            <Select value={mode} style={{ width: 200 }}
              options={[
                { value: 'light', label: t('header.themeLight') },
                { value: 'dark', label: t('header.themeDark') },
                { value: 'system', label: t('header.themeSystem') },
              ]}
              onChange={(v) => setMode(v)} />
          </Form.Item>
          <Form.Item label={t('admin.langLabel')}>
            <Select value={ti18n.language} style={{ width: 200 }}
              options={LANGS.map((l) => ({ value: l.code, label: l.label }))}
              onChange={(v) => setLang(v)} />
          </Form.Item>
        </Form>
      </Card>
      <LoginSecurityCard />
      <TimeConfigCard />
    </div>
  )
}

/** 登录安全：IP 白名单 + 防暴破锁定参数（DB 持久化，缺省 5 次/30 次/15 分钟） */
function LoginSecurityCard() {
  const { t } = useTranslation()
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [source, setSource] = useState('default')
  const load = () => api.get('/api/admin/login-config').then((r) => {
    setSource(r.data.source)
    form.setFieldsValue({
      ip_whitelist_enabled: r.data.ip_whitelist_enabled, ip_whitelist: r.data.ip_whitelist,
      user_attempts: r.data.user_attempts, ip_attempts: r.data.ip_attempts,
      lock_minutes: r.data.lock_minutes,
    })
  }).catch(() => {})
  useEffect(() => { load() }, [])
  const save = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      await api.put('/api/admin/login-config', v)
      message.success(t('admin.saved')); load()
    } finally { setSaving(false) }
  }
  return (
    <Card size="small" style={{ maxWidth: 560, marginTop: 12 }}
      title={<Space>{t('admin.lcTitle')}
        {source === 'default' && <Tag>{t('admin.lcDefault')}</Tag>}</Space>}
      extra={<Button type="primary" size="small" loading={saving} onClick={save}>{t('admin.saveCfg')}</Button>}>
      <Form form={form} layout="vertical"
        initialValues={{ ip_whitelist_enabled: false, ip_whitelist: '', user_attempts: 5, ip_attempts: 30, lock_minutes: 15 }}>
        <Space size={8} align="center" style={{ marginBottom: 8 }}>
          <Form.Item name="ip_whitelist_enabled" valuePropName="checked" noStyle><Switch size="small" /></Form.Item>
          <span>{t('admin.lcWhitelist')}</span>
        </Space>
        <Form.Item name="ip_whitelist" style={{ marginBottom: 12 }}>
          <Input.TextArea rows={3} placeholder={t('admin.lcWhitelistPh')} />
        </Form.Item>
        <div style={{ color: '#999', fontSize: 12, marginTop: -8, marginBottom: 12 }}>{t('admin.lcWhitelistHint')}</div>
        <Row gutter={12}>
          <Col span={8}><Form.Item name="user_attempts" label={t('admin.lcUserAttempts')}><InputNumber size="small" style={{ width: '100%' }} min={1} max={100} /></Form.Item></Col>
          <Col span={8}><Form.Item name="ip_attempts" label={t('admin.lcIpAttempts')}><InputNumber size="small" style={{ width: '100%' }} min={1} max={1000} /></Form.Item></Col>
          <Col span={8}><Form.Item name="lock_minutes" label={t('admin.lcLockMinutes')}><InputNumber size="small" style={{ width: '100%' }} min={1} max={1440} /></Form.Item></Col>
        </Row>
      </Form>
    </Card>
  )
}

/** 常见 IANA 时区（当前宿主时区不在列表时自动补入首项） */
const TZ_OPTIONS = ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Singapore', 'Asia/Tokyo', 'Asia/Seoul',
  'Asia/Kolkata', 'Asia/Dubai', 'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Moscow',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'America/Sao_Paulo',
  'Australia/Sydney', 'Pacific/Auckland', 'Etc/UTC']

function tzOffsetLabel(tz: string): string {
  try {
    const part = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' })
      .formatToParts(new Date()).find((p) => p.type === 'timeZoneName')
    return part ? ` (${part.value})` : ''
  } catch { return '' }
}

/** 时间与 NTP：集群时区（立即应用宿主）+ NTP 服务器（写入宿主时钟同步配置，重启服务生效） */
function TimeConfigCard() {
  const { t } = useTranslation()
  const [form] = Form.useForm()
  const [state, setState] = useState<any>(null)
  const [saving, setSaving] = useState(false)
  const [probing, setProbing] = useState(false)
  const [probeRes, setProbeRes] = useState<any[]>([])
  const [drift, setDrift] = useState(0)      // 服务器时钟 - 本地时钟（ms），用于本地外推宿主时间
  const [clock, setClock] = useState(() => Date.now())
  const [restartHint, setRestartHint] = useState('')
  const load = () => api.get('/api/admin/time-config').then((r) => {
    setState(r.data)
    setDrift((r.data.epoch || 0) * 1000 - Date.now())
    form.setFieldsValue({ timezone: r.data.timezone, ntp_servers: (r.data.ntp_servers || []).join(' ') })
  }).catch(() => {})
  useEffect(() => {
    load()
    const x = setInterval(() => setClock(Date.now() + drift), 1000)
    return () => clearInterval(x)
  }, [drift])
  const probe = async () => {
    const v = form.getFieldValue('ntp_servers') || ''
    if (!v.trim()) { message.warning(t('admin.tcNtpPh')); return }
    setProbing(true); setProbeRes([])
    try {
      const r = await api.post('/api/admin/time-config/probe', { ntp_servers: v })
      setProbeRes(r.data.results || [])
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    } finally { setProbing(false) }
  }
  const save = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const r = await api.put('/api/admin/time-config', v)
      const a = r.data.applied || {}
      if (a.timezone?.ok) message.success(a.timezone.detail)
      else if (a.timezone) message.error(a.timezone.detail || t('admin.tcTzFail'))
      if (a.ntp?.ok) {
        message.success(a.ntp.detail)
        setRestartHint(a.ntp.restart_hint || '')
      } else if (a.ntp) {
        message.error(a.ntp.detail || t('admin.tcNtpFail'))
        setRestartHint('')
      }
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    } finally { setSaving(false) }
  }
  const tz = state?.timezone || 'Etc/UTC'
  const fmtClock = (ms: number) => {
    try {
      return new Intl.DateTimeFormat(undefined, {
        timeZone: tz, hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      }).format(new Date(ms))
    } catch { return new Date(ms).toLocaleString() }
  }
  const hostTz = state?.host?.timezone
  const tzOptions = (hostTz && !TZ_OPTIONS.includes(hostTz) ? [hostTz, ...TZ_OPTIONS] : TZ_OPTIONS)
    .map((z) => ({ value: z, label: `${z}${tzOffsetLabel(z)}` }))
  const ntp = state?.host?.ntp || {}
  return (
    <Card size="small" style={{ maxWidth: 560, marginTop: 12 }} title={<Space><ClockCircleOutlined />{t('admin.tcTitle')}</Space>}
      extra={<Button type="primary" size="small" loading={saving} onClick={save}>{t('admin.tcSave')}</Button>}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 8 }}>
        <span style={{ fontSize: 12, color: '#999' }}>{t('admin.tcNow')}</span>
        <Space size={8}>
          <span style={{ fontSize: 16, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{state ? fmtClock(clock) : '…'}</span>
          <Tag>{hostTz || '…'}</Tag>
        </Space>
      </div>
      <Form form={form} layout="vertical">
        <Form.Item name="timezone" label={t('admin.tcTz')} rules={[{ required: true }]}>
          <Select showSearch optionFilterProp="label" options={tzOptions} />
        </Form.Item>
        <Form.Item name="ntp_servers" label={t('admin.tcNtp')}>
          <Input.TextArea rows={2} placeholder={t('admin.tcNtpPh')} />
        </Form.Item>
      </Form>
      <div style={{ color: '#999', fontSize: 12, marginTop: -8, marginBottom: 8 }}>{t('admin.tcNtpHint')}</div>
      <Space wrap size={8} style={{ marginBottom: 8 }}>
        <Button size="small" loading={probing} onClick={probe}>{t('admin.tcProbe')}</Button>
        {probeRes.map((p) => (
          <Tooltip key={p.server} title={p.detail || ''}>
            <Tag color={p.ok ? 'success' : 'error'}>
              {p.server} {p.ok ? `${t('admin.tcProbeOk')} ${p.offset_ms >= 0 ? '+' : ''}${p.offset_ms}ms` : t('admin.tcProbeFail')}
            </Tag>
          </Tooltip>
        ))}
      </Space>
      {state && (
        <div style={{ fontSize: 12, color: '#888', background: 'rgba(0,0,0,0.025)', borderRadius: 6, padding: '6px 10px' }}>
          <div>{t('admin.tcImpl')}: <Tag>{ntp.impl || '-'}</Tag>
            {t('admin.tcCfgServers')}: <code>{(ntp.configured || []).join(', ') || '-'}</code></div>
        </div>
      )}
      {restartHint && <div style={{ marginTop: 8 }}><AlertLine>{t('admin.tcRestart')}: <code>{restartHint}</code></AlertLine></div>}
    </Card>
  )
}
