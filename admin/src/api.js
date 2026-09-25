import axios from 'axios'
import { ElMessage } from 'element-plus'
import { auth, clearAuth } from './store'
import router from './router'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 15000
})

api.interceptors.request.use((config) => {
  if (auth.token) {
    config.headers.Authorization = `Bearer ${auth.token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    if (status === 401) {
      clearAuth()
      if (router.currentRoute.value.path !== '/login') {
        ElMessage.error('登录已过期，请重新登录')
        router.push('/login')
      }
    }
    return Promise.reject(error)
  }
)

// 统一提取后端错误信息（FastAPI 的 detail 可能是字符串或数组）
export function errMsg(error, fallback = '操作失败') {
  const detail = error.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length) return detail[0].msg || fallback
  return fallback
}

export default api
