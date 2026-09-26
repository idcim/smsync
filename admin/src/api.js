import axios from 'axios'
import { ElMessage } from 'element-plus'
import { auth, clearAuth, setTokens } from './store'
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

// 并发 401 时共享同一次刷新请求，避免多次 refresh
let refreshPromise = null

async function refreshTokens() {
  if (!refreshPromise) {
    // 用裸 axios 发刷新请求，不经过本实例的拦截器，避免 401 重试循环
    refreshPromise = axios
      .post('/api/v1/auth/refresh', { refresh_token: auth.refreshToken })
      .then(({ data }) => {
        setTokens(data.access_token, data.refresh_token)
      })
      .finally(() => {
        refreshPromise = null
      })
  }
  return refreshPromise
}

function forceLogout() {
  clearAuth()
  if (router.currentRoute.value.path !== '/login') {
    ElMessage.error('登录已过期，请重新登录')
    router.push('/login')
  }
}

api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const status = error.response?.status
    const config = error.config
    if (config?._noRefresh) {
      // 登录等自带 401 处理的请求：不刷新、不强制登出，交给调用方处理
      return Promise.reject(error)
    }
    if (status === 401 && config && !config._retried && auth.refreshToken) {
      config._retried = true // 每个请求只重试一次
      try {
        await refreshTokens()
      } catch {
        forceLogout()
        return Promise.reject(error)
      }
      // 刷新成功，用新 token 重试原请求
      config.headers.Authorization = `Bearer ${auth.token}`
      return api.request(config)
    }
    if (status === 401) {
      forceLogout()
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
