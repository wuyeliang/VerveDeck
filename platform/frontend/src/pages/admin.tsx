import React, { useEffect, useState } from 'react'
import {
  Tabs, Table, Button, Space, Tag, Modal, Form, Input, InputNumber, Select, message, Popconfirm, Card,
  Switch, Row, Col, Tooltip,
} from 'antd'
import { PlusOutlined, CopyOutlined, ClockCircleOutlined, SafetyCertificateOutlined } from '@ant-design/icons'
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
        // 集群升级已迁至「集群运维 > 集群升级」（/cluster/upgrade，pages/cluster-upgrade.tsx）
        { key: 'settings', label: t('admin.tabSettings'), children: <SettingsTab /> },
      ]} />
    </div>
  )
}


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
      <GatewayRateCard />
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

/** 网关限流：nginx 三档速率 + 单 IP 并发（DB 持久化；保存后渲染 zones.conf 并 nginx reload 热生效，
 *  校验失败自动回滚。未配置=默认 5/30/60 r/s + 并发 50，见 docs/11-安全加固） */
function GatewayRateCard() {
  const { t } = useTranslation()
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [source, setSource] = useState('default')
  const load = () => api.get('/api/admin/gateway-rate').then((r) => {
    setSource(r.data.source)
    form.setFieldsValue({
      login_rate: r.data.login_rate, api_rate: r.data.api_rate,
      v1_rate: r.data.v1_rate, conn_limit: r.data.conn_limit,
    })
  }).catch(() => {})
  useEffect(() => { load() }, [])
  const save = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const r = await api.put('/api/admin/gateway-rate', v)
      if (r.data.applied?.ok) message.success(r.data.applied.detail || t('admin.grApplied'))
      else message.warning(r.data.applied?.detail || t('admin.saved'))
      load()
    } catch (e: any) {
      message.error(e.response?.data?.detail || t('api.requestFailed'))
    } finally { setSaving(false) }
  }
  return (
    <Card size="small" style={{ maxWidth: 560, marginTop: 12 }}
      title={<Space><SafetyCertificateOutlined />{t('admin.grTitle')}
        {source === 'default' && <Tag>{t('admin.lcDefault')}</Tag>}</Space>}
      extra={<Button type="primary" size="small" loading={saving} onClick={save}>{t('admin.saveCfg')}</Button>}>
      <Form form={form} layout="vertical"
        initialValues={{ login_rate: 5, api_rate: 30, v1_rate: 60, conn_limit: 50 }}>
        <Row gutter={12}>
          <Col span={8}>
            <Form.Item name="login_rate" label={t('admin.grLoginRate')}><InputNumber size="small" style={{ width: '100%' }} min={1} max={1000} /></Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="api_rate" label={t('admin.grApiRate')}><InputNumber size="small" style={{ width: '100%' }} min={1} max={5000} /></Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="v1_rate" label={t('admin.grV1Rate')}><InputNumber size="small" style={{ width: '100%' }} min={1} max={5000} /></Form.Item>
          </Col>
        </Row>
        <Form.Item name="conn_limit" label={t('admin.grConnLimit')}>
          <InputNumber size="small" style={{ width: 200 }} min={1} max={10000} />
        </Form.Item>
      </Form>
      <div style={{ color: '#999', fontSize: 12, marginTop: -4 }}>{t('admin.grHint')}</div>
    </Card>
  )
}
