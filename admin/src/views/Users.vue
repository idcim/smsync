<template>
  <div>
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">新建用户</el-button>
      <el-button @click="loadUsers" :loading="loading">刷新</el-button>
    </div>

    <el-table :data="users" v-loading="loading" border>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="username" label="用户名" />
      <el-table-column label="角色" width="100">
        <template #default="{ row }">
          <el-tag :type="row.role === 'admin' ? 'danger' : 'info'">
            {{ row.role === 'admin' ? '管理员' : '普通用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.disabled ? 'warning' : 'success'">
            {{ row.disabled ? '已禁用' : '正常' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="200">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="320">
        <template #default="{ row }">
          <el-button size="small" @click="openReset(row)">重置密码</el-button>
          <el-button
            size="small"
            :type="row.disabled ? 'success' : 'warning'"
            :disabled="row.id === auth.user?.id"
            @click="toggleDisabled(row)"
          >
            {{ row.disabled ? '启用' : '禁用' }}
          </el-button>
          <el-button
            size="small"
            type="danger"
            :disabled="row.id === auth.user?.id"
            @click="onDelete(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建用户 -->
    <el-dialog v-model="createVisible" title="新建用户" width="420px">
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-width="80px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="createForm.username" placeholder="字母/数字/下划线开头，2-32 位" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="createForm.password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-radio-group v-model="createForm.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onCreate">确定</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetVisible" :title="`重置密码：${resetTarget?.username || ''}`" width="420px">
      <el-form ref="resetFormRef" :model="resetForm" :rules="resetRules" label-width="80px">
        <el-form-item label="新密码" prop="password">
          <el-input v-model="resetForm.password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onReset">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api, { errMsg } from '../api'
import { auth } from '../store'

const users = ref([])
const loading = ref(false)
const submitting = ref(false)

const createVisible = ref(false)
const createFormRef = ref()
const createForm = reactive({ username: '', password: '', role: 'user' })
const createRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[a-zA-Z0-9_][a-zA-Z0-9_.-]{1,31}$/, message: '字母/数字/下划线开头，2-32 位，可含 . - _', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' }
  ],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }]
}

const resetVisible = ref(false)
const resetTarget = ref(null)
const resetFormRef = ref()
const resetForm = reactive({ password: '' })
const resetRules = {
  password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' }
  ]
}

function formatTime(t) {
  if (!t) return ''
  const d = new Date(t)
  return Number.isNaN(d.getTime()) ? t : d.toLocaleString('zh-CN', { hour12: false })
}

async function loadUsers() {
  loading.value = true
  try {
    const { data } = await api.get('/users')
    users.value = data.items || []
  } catch (e) {
    ElMessage.error(errMsg(e, '加载用户列表失败'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  createForm.username = ''
  createForm.password = ''
  createForm.role = 'user'
  createVisible.value = true
}

async function onCreate() {
  await createFormRef.value.validate().catch(() => Promise.reject())
  submitting.value = true
  try {
    await api.post('/users', { ...createForm })
    ElMessage.success('创建成功')
    createVisible.value = false
    loadUsers()
  } catch (e) {
    ElMessage.error(errMsg(e, '创建失败'))
  } finally {
    submitting.value = false
  }
}

function openReset(row) {
  resetTarget.value = row
  resetForm.password = ''
  resetVisible.value = true
}

async function onReset() {
  await resetFormRef.value.validate().catch(() => Promise.reject())
  submitting.value = true
  try {
    await api.patch(`/users/${resetTarget.value.id}`, { password: resetForm.password })
    ElMessage.success('密码已重置')
    resetVisible.value = false
  } catch (e) {
    ElMessage.error(errMsg(e, '重置失败'))
  } finally {
    submitting.value = false
  }
}

async function toggleDisabled(row) {
  try {
    await api.patch(`/users/${row.id}`, { disabled: !row.disabled })
    ElMessage.success(row.disabled ? '已启用' : '已禁用')
    loadUsers()
  } catch (e) {
    ElMessage.error(errMsg(e, '操作失败'))
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除用户「${row.username}」吗？此操作不可恢复。`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消'
    })
  } catch {
    return
  }
  try {
    await api.delete(`/users/${row.id}`)
    ElMessage.success('已删除')
    loadUsers()
  } catch (e) {
    ElMessage.error(errMsg(e, '删除失败'))
  }
}

onMounted(loadUsers)
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  gap: 12px;
}
</style>
