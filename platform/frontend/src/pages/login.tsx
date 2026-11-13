import React, { useState } from 'react'
import { Form, Input, Button, Card, message, Typography } from 'antd'
import { UserOutlined, LockOutlined, RocketOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api'

export default function Login() {
  const nav = useNavigate()
  const { t } = useTranslation()
  const [loading, setLoading] = useState(false)
  const onFinish = async (v: any) => {
    setLoading(true)
    try {
      const r = await api.post('/api/auth/login', v)
      localStorage.setItem('token', r.data.token)
      message.success(t('login.welcome'))
      nav('/')
    } catch {
      /* 错误提示由拦截器处理 */
    } finally {
      setLoading(false)
    }
  }
  return (
    <div style={{ height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'linear-gradient(135deg,#e6f0ff 0%,#f5f7fa 100%)' }}>
      <Card style={{ width: 380, boxShadow: '0 8px 30px rgba(0,0,0,.08)' }}>
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <RocketOutlined style={{ fontSize: 40, color: '#1677ff' }} />
          <Typography.Title level={3} style={{ margin: '8px 0 0' }}>VerveDeck</Typography.Title>
          <Typography.Text type="secondary">{t('login.subtitle')}</Typography.Text>
        </div>
        <Form onFinish={onFinish} size="large">
          <Form.Item name="username" rules={[{ required: true, message: t('login.requiredUser') }]}>
            <Input prefix={<UserOutlined />} placeholder={t('login.username')} />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: t('login.requiredPw') }]}>
            <Input.Password prefix={<LockOutlined />} placeholder={t('login.password')} />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>{t('login.loginBtn')}</Button>
        </Form>
      </Card>
    </div>
  )
}
