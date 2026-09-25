<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <h2 class="login-title">SMSync 管理后台</h2>
      <el-form :model="form" @submit.prevent="onSubmit">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            show-password
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" class="login-btn" :loading="loading" @click="onSubmit">
          登 录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api, { errMsg } from '../api'
import { setAuth } from '../store'

const router = useRouter()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

async function onSubmit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const { data } = await api.post('/auth/login', {
      username: form.username,
      password: form.password
    })
    setAuth(data.access_token, data.user)
    router.push('/')
  } catch (e) {
    const status = e.response?.status
    if (status === 429) {
      ElMessage.error(errMsg(e, '失败次数过多，请稍后再试'))
    } else {
      ElMessage.error(errMsg(e, '登录失败，请检查用户名和密码'))
    }
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f0f2f5;
}
.login-card {
  width: 360px;
}
.login-title {
  text-align: center;
  margin: 0 0 24px;
}
.login-btn {
  width: 100%;
}
</style>
