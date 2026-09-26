import { reactive } from 'vue'

const TOKEN_KEY = 'smsync_admin_jwt'
const REFRESH_KEY = 'smsync_admin_refresh'
const USER_KEY = 'smsync_admin_user'

export const auth = reactive({
  token: localStorage.getItem(TOKEN_KEY) || '',
  refreshToken: localStorage.getItem(REFRESH_KEY) || '',
  user: JSON.parse(localStorage.getItem(USER_KEY) || 'null')
})

export function setAuth(token, refreshToken, user) {
  auth.token = token
  auth.refreshToken = refreshToken
  auth.user = user
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(REFRESH_KEY, refreshToken)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

// 刷新成功时只更新两个 token（user 信息不变）
export function setTokens(token, refreshToken) {
  auth.token = token
  auth.refreshToken = refreshToken
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(REFRESH_KEY, refreshToken)
}

export function clearAuth() {
  auth.token = ''
  auth.refreshToken = ''
  auth.user = null
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_KEY)
  localStorage.removeItem(USER_KEY)
}

export function isLoggedIn() {
  return !!auth.token
}

export function isAdmin() {
  return auth.user && auth.user.role === 'admin'
}
