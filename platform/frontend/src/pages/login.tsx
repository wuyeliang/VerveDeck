import React, { useEffect, useRef, useState } from 'react'
import { Form, Input, Button, Card, message, Typography } from 'antd'
import { UserOutlined, LockOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

export default function Login() {
  const nav = useNavigate()
  const { t } = useTranslation()
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm()
  const submittingRef = useRef(false)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 用户名 + 密码均非空时，停止输入 600ms 自动提交（登录成功后 onFinish 内 nav('/') 自动跳转）
  const maybeAutoSubmit = () => {
    const user = form.getFieldValue('username')
    const pw = form.getFieldValue('password')
    if (!user || !pw) return
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => form.submit(), 600)
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
      /* 错误提示由拦截器处理 */
    } finally {
      submittingRef.current = false
      setLoading(false)
    }
  }
  return (
    <div style={{ height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'linear-gradient(135deg,#e6f0ff 0%,#f5f7fa 100%)' }}>
      <Card style={{ width: 380, boxShadow: '0 8px 30px rgba(0,0,0,.08)' }}>
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
