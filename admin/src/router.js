import { createRouter, createWebHashHistory } from 'vue-router'
import { isLoggedIn, isAdmin } from './store'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { title: '登录' } },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    children: [
      { path: '', redirect: '/users' },
      { path: 'users', name: 'users', component: () => import('./views/Users.vue'), meta: { title: '用户管理', admin: true } },
      { path: 'devices', name: 'devices', component: () => import('./views/Devices.vue'), meta: { title: '设备管理', admin: true } },
      { path: 'password', name: 'password', component: () => import('./views/Password.vue'), meta: { title: '修改密码' } }
    ]
  },
  { path: '/:pathMatch(.*)*', redirect: '/' }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

router.beforeEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} - SMSync 管理后台` : 'SMSync 管理后台'
  if (to.path === '/login') {
    return isLoggedIn() ? '/' : true
  }
  if (!isLoggedIn()) {
    return '/login'
  }
  if (to.meta.admin && !isAdmin()) {
    return '/password'
  }
  return true
})

export default router
