import axios from 'axios'
import { message } from 'antd'
import i18n from './i18n'

export const api = axios.create({ baseURL: '', timeout: 120000 })

api.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

api.interceptors.response.use(
  (r) => r,
  (err) => {
    const msg = err.response?.data?.detail || err.message || i18n.t('api.requestFailed')
    if (err.response?.status !== 401) message.error(typeof msg === 'string' ? msg : JSON.stringify(msg))
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      location.href = '/login'
    }
    return Promise.reject(err)
  },
)

export const getProjects = () => api.get('/api/admin/projects').then((r) => r.data)
export const token = () => localStorage.getItem('token') || ''
