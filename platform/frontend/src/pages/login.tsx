import React, { useEffect, useRef, useState } from 'react'
import { Form, Input, Button, Card, message, Typography, theme as antTheme } from 'antd'
import { UserOutlined, LockOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useTheme } from '../theme'

export default function Login() {
  const nav = useNavigate()
  const { t } = useTranslation()
  const { dark } = useTheme()
  const { token } = antTheme.useToken()
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm()
  const submittingRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const failedPwRef = useRef('')

  // 用户名 + 密码均非空、且密码达到最小长度、停止输入 1.2s 后自动提交（登录成功后 onFinish 内 nav('/') 自动跳转）。
  // 登录失败后，同一密码不再自动重提，需用户改动或手动点按钮，避免「还没输完/刚失败就刷新」。
  const maybeAutoSubmit = () => {
    const user = form.getFieldValue('username')
    const pw = form.getFieldValue('password')
    if (!user || !pw || pw.length < 6) return
    if (pw === failedPwRef.current) return
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => form.submit(), 1200)
  }

  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const onFinish = async (v: any) => {
    if (submittingRef.current) return
    if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null }
    submittingRef.current = true
    setLoading(true)
    try {
      const r = await api.post('/api/auth/login', v)
      localStorage.setItem('token', r.data.token)
      message.success(t('login.welcome'))
      nav('/')
    } catch {
      failedPwRef.current = v?.password || ''
    } finally {
      submittingRef.current = false
      setLoading(false)
    }
  }
  return (
    <div style={{ height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: dark ? 'linear-gradient(135deg,#141414 0%,#1f1f1f 100%)' : 'linear-gradient(135deg,#e6f0ff 0%,#f5f7fa 100%)' }}>
      <Card style={{ width: 380, boxShadow: '0 8px 30px rgba(0,0,0,.16)', background: token.colorBgContainer }}>
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <img src="/logo.svg" alt="VerveDeck" style={{ width: 56, height: 56, borderRadius: 14 }} />
          <Typography.Title level={3} style={{ margin: '8px 0 0' }}>VerveDeck</Typography.Title>
          <Typography.Text type="secondary">{t('login.subtitle')}</Typography.Text>
        </div>
        <Form form={form} onFinish={onFinish} size="large" autoComplete="off">
          <Form.Item name="username" rules={[{ required: true, message: t('login.requiredUser') }]}>
            <Input prefix={<UserOutlined />} placeholder={t('login.username')} onChange={maybeAutoSubmit} />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: t('login.requiredPw') }]}>
            <Input.Password prefix={<LockOutlined />} placeholder={t('login.password')} onChange={maybeAutoSubmit} />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>{t('login.loginBtn')}</Button>
        </Form>
      </Card>
    </div>
  )
}
