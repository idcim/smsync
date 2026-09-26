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
        <el-form-item>
          <div class="captcha-row">
            <el-input
              v-model="form.captcha_text"
              placeholder="验证码"
              maxlength="8"
              @keyup.enter="onSubmit"
            />
            <el-tooltip content="点击刷新验证码" placement="top">
              <img
                v-if="captchaImage"
                class="captcha-img"
                :src="captchaImage"
                alt="验证码"
                @click="loadCaptcha"
              />
              <div v-else class="captcha-img captcha-loading" @click="loadCaptcha">
                {{ captchaFailed ? '加载失败，点击重试' : '加载中…' }}
              </div>
            </el-tooltip>
          </div>
        </el-form-item>
        <el-button type="primary" class="login-btn" :loading="loading" @click="onSubmit">
          登 录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api, { errMsg } from '../api'
import { setAuth } from '../store'

const router = useRouter()
const loading = ref(false)
const form = reactive({ username: '', password: '', captcha_text: '' })
const captchaId = ref('')
const captchaImage = ref('')
const captchaFailed = ref(false)

async function loadCaptcha() {
  captchaImage.value = ''
  captchaFailed.value = false
  try {
    const { data } = await api.get('/auth/captcha')
    captchaId.value = data.captcha_id
    captchaImage.value = data.image
  } catch (e) {
    captchaFailed.value = true
    ElMessage.error(errMsg(e, '验证码加载失败，请点击图片重试'))
  }
}

function refreshCaptcha() {
  form.captcha_text = ''
  loadCaptcha()
}

async function onSubmit() {
  if (!form.username || !form.password || !form.captcha_text) {
    ElMessage.warning('请输入用户名、密码和验证码')
    return
  }
  loading.value = true
  try {
    const { data } = await api.post(
      '/auth/login',
      {
        username: form.username,
        password: form.password,
        captcha_id: captchaId.value,
        captcha_text: form.captcha_text
      },
      { _noRefresh: true } // 登录失败由本页自行处理，不走 token 刷新
    )
    setAuth(data.access_token, data.refresh_token, data.user)
    router.push('/')
  } catch (e) {
    const status = e.response?.status
    if (status === 400) {
      // 验证码错误或过期（旧的已被销毁，必须换新）
      ElMessage.error('验证码错误或已过期')
      refreshCaptcha()
    } else if (status === 401) {
      ElMessage.error(errMsg(e, '用户名或密码错误'))
      refreshCaptcha()
    } else if (status === 429) {
      ElMessage.error(errMsg(e, '失败次数过多，请稍后再试'))
    } else {
      ElMessage.error(errMsg(e, '登录失败，请稍后重试'))
    }
  } finally {
    loading.value = false
  }
}

onMounted(loadCaptcha)
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
.captcha-row {
  display: flex;
  gap: 8px;
  width: 100%;
}
.captcha-img {
  height: 32px;
  width: 120px;
  flex-shrink: 0;
  cursor: pointer;
  border-radius: 4px;
  border: 1px solid #dcdfe6;
  object-fit: cover;
}
.captcha-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: #909399;
  background: #f5f7fa;
}
</style>
