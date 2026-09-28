<template>
  <div>
    <div class="toolbar">
      <el-button type="primary" @click="openCreate">新建设备</el-button>
      <el-button @click="loadDevices" :loading="loading">刷新</el-button>
    </div>

    <el-table :data="devices" v-loading="loading" border>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="name" label="设备名称" />
      <el-table-column v-if="isAdmin()" label="属主" width="140">
        <template #default="{ row }">
          <span v-if="row.owner_id != null">{{ ownerName(row.owner_id) }}</span>
          <span v-else class="owner-none">未绑定</span>
        </template>
      </el-table-column>
      <el-table-column label="设备码" min-width="220">
        <template #default="{ row }">
          <div v-if="row.device_key" class="key-cell">
            <span class="key-inline">{{ row.device_key }}</span>
            <el-button size="small" link type="primary" @click="copyText(row.device_key)">
              复制
            </el-button>
          </div>
          <!-- v2.3.0 时期创建的设备：不存明文，从未使用也无法查看 -->
          <span v-else-if="!row.last_seen_at" class="key-expired">码已失效，请重置</span>
          <span v-else class="key-hidden">已使用，已隐藏</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.disabled ? 'warning' : 'success'">
            {{ row.disabled ? '已禁用' : '正常' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="200">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="最近上线" width="200">
        <template #default="{ row }">
          {{ row.last_seen_at ? formatTime(row.last_seen_at) : '从未' }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="400">
        <template #default="{ row }">
          <el-button size="small" @click="openRename(row)">重命名</el-button>
          <el-button v-if="isAdmin()" size="small" @click="openRebind(row)">改绑</el-button>
          <el-button size="small" type="primary" plain @click="onRegenerate(row)">重置设备码</el-button>
          <el-button
            size="small"
            :type="row.disabled ? 'success' : 'warning'"
            @click="toggleDisabled(row)"
          >
            {{ row.disabled ? '启用' : '禁用' }}
          </el-button>
          <el-button size="small" type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建设备 -->
    <el-dialog v-model="createVisible" title="新建设备" width="420px">
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-width="80px">
        <el-form-item label="设备名称" prop="name">
          <el-input v-model="createForm.name" placeholder="例如：机房 EC20 采集端" maxlength="64" />
        </el-form-item>
        <el-form-item v-if="isAdmin()" label="属主">
          <el-select v-model="createForm.owner_id" style="width: 100%">
            <el-option
              v-for="u in userOptions"
              :key="u.id"
              :label="u.username"
              :value="u.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 设备码展示（创建成功 / 重置成功共用） -->
    <el-dialog
      v-model="keyVisible"
      :title="keyTitle"
      width="520px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
    >
      <el-alert
        type="warning"
        :closable="false"
        title="设备首次上线前可在本页面随时查看设备码；设备一旦使用，设备码将自动隐藏。"
        class="key-alert"
      />
      <div class="key-box">
        <span class="key-text">{{ createdKey }}</span>
      </div>
      <template #footer>
        <el-button type="primary" @click="copyText(createdKey, true)">
          {{ copied ? '已复制 ✓' : '复制设备码' }}
        </el-button>
        <el-button @click="keyVisible = false">我已保存，关闭</el-button>
      </template>
    </el-dialog>

    <!-- 重命名 -->
    <el-dialog v-model="renameVisible" title="重命名设备" width="420px">
      <el-form ref="renameFormRef" :model="renameForm" :rules="createRules" label-width="80px">
        <el-form-item label="设备名称" prop="name">
          <el-input v-model="renameForm.name" maxlength="64" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onRename">确定</el-button>
      </template>
    </el-dialog>

    <!-- 改绑属主（仅 admin） -->
    <el-dialog v-model="rebindVisible" :title="`改绑属主：${rebindTarget?.name || ''}`" width="420px">
      <el-form label-width="80px">
        <el-form-item label="属主">
          <el-select v-model="rebindOwnerId" style="width: 100%">
            <el-option label="未绑定" :value="null" />
            <el-option
              v-for="u in userOptions"
              :key="u.id"
              :label="u.username"
              :value="u.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rebindVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="onRebind">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api, { errMsg } from '../api'
import { auth, isAdmin } from '../store'

const devices = ref([])
const loading = ref(false)
const submitting = ref(false)

// admin 专用：用户列表（id→username 映射、属主下拉选项）
const userOptions = ref([])

const createVisible = ref(false)
const createFormRef = ref()
const createForm = reactive({ name: '', owner_id: null })
const createRules = {
  name: [{ required: true, message: '请输入设备名称', trigger: 'blur' }]
}

const keyVisible = ref(false)
const keyTitle = ref('设备创建成功')
const createdKey = ref('')
const copied = ref(false)

// 弹出设备码结果对话框（创建/重置共用）
function showKeyDialog(title, key) {
  keyTitle.value = title
  createdKey.value = key || ''
  copied.value = false
  keyVisible.value = true
}

const renameVisible = ref(false)
const renameTarget = ref(null)
const renameFormRef = ref()
const renameForm = reactive({ name: '' })

const rebindVisible = ref(false)
const rebindTarget = ref(null)
const rebindOwnerId = ref(null)

function ownerName(id) {
  const u = userOptions.value.find((x) => x.id === id)
  return u ? u.username : `#${id}`
}

// 仅 admin 拉用户列表（普通用户调 /users 会 403）
async function loadUsers() {
  try {
    const { data } = await api.get('/users')
    userOptions.value = data.items || []
  } catch (e) {
    ElMessage.error(errMsg(e, '加载用户列表失败'))
  }
}

function formatTime(t) {
  if (!t) return ''
  const d = new Date(t)
  return Number.isNaN(d.getTime()) ? t : d.toLocaleString('zh-CN', { hour12: false })
}

async function loadDevices() {
  loading.value = true
  try {
    const { data } = await api.get('/devices')
    devices.value = data.items || []
  } catch (e) {
    ElMessage.error(errMsg(e, '加载设备列表失败'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  createForm.name = ''
  createForm.owner_id = isAdmin() ? auth.user?.id ?? null : null // admin 默认归属自己
  createVisible.value = true
}

async function onCreate() {
  await createFormRef.value.validate().catch(() => Promise.reject())
  submitting.value = true
  try {
    // owner_id 仅 admin 可指定，普通用户不传（owner 由后端设为本人）
    const body = { name: createForm.name }
    if (isAdmin() && createForm.owner_id != null) {
      body.owner_id = createForm.owner_id
    }
    const { data } = await api.post('/devices', body)
    createVisible.value = false
    showKeyDialog('设备创建成功', data.device_key)
    loadDevices()
  } catch (e) {
    ElMessage.error(errMsg(e, '创建失败'))
  } finally {
    submitting.value = false
  }
}

// 复制到剪贴板：优先 navigator.clipboard，非安全上下文用 execCommand 兜底
async function copyText(text, markCopied = false) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    if (markCopied) copied.value = true
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择文本复制')
  }
}

function openRename(row) {
  renameTarget.value = row
  renameForm.name = row.name
  renameVisible.value = true
}

async function onRename() {
  await renameFormRef.value.validate().catch(() => Promise.reject())
  submitting.value = true
  try {
    await api.patch(`/devices/${renameTarget.value.id}`, { name: renameForm.name })
    ElMessage.success('已重命名')
    renameVisible.value = false
    loadDevices()
  } catch (e) {
    ElMessage.error(errMsg(e, '重命名失败'))
  } finally {
    submitting.value = false
  }
}

function openRebind(row) {
  rebindTarget.value = row
  rebindOwnerId.value = row.owner_id ?? null
  rebindVisible.value = true
}

async function onRebind() {
  submitting.value = true
  try {
    // 显式传 null 表示解绑
    await api.patch(`/devices/${rebindTarget.value.id}`, { owner_id: rebindOwnerId.value })
    ElMessage.success('已更新归属')
    rebindVisible.value = false
    loadDevices()
  } catch (e) {
    ElMessage.error(errMsg(e, '改绑失败'))
  } finally {
    submitting.value = false
  }
}

async function onRegenerate(row) {
  try {
    await ElMessageBox.confirm(
      `确定为设备「${row.name}」重置设备码吗？旧设备码将立即失效，需用新码重新配置采集端。`,
      '重置设备码',
      { type: 'warning', confirmButtonText: '重置', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    const { data } = await api.post(`/devices/${row.id}/regenerate`)
    showKeyDialog(`设备码已重置：${row.name}`, data.device_key)
    loadDevices()
  } catch (e) {
    ElMessage.error(errMsg(e, '重置失败'))
  }
}

async function toggleDisabled(row) {
  try {
    await api.patch(`/devices/${row.id}`, { disabled: !row.disabled })
    ElMessage.success(row.disabled ? '已启用' : '已禁用')
    loadDevices()
  } catch (e) {
    ElMessage.error(errMsg(e, '操作失败'))
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除设备「${row.name}」吗？删除后该设备码将立即失效，不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }
  try {
    await api.delete(`/devices/${row.id}`)
    ElMessage.success('已删除')
    loadDevices()
  } catch (e) {
    ElMessage.error(errMsg(e, '删除失败'))
  }
}

onMounted(() => {
  loadDevices()
  if (isAdmin()) loadUsers()
})
</script>

<style scoped>
.toolbar {
  margin-bottom: 16px;
  display: flex;
  gap: 12px;
}
.key-alert {
  margin-bottom: 16px;
}
.key-box {
  background: #f5f7fa;
  border: 1px dashed #dcdfe6;
  border-radius: 6px;
  padding: 16px;
  text-align: center;
  word-break: break-all;
}
.key-text {
  font-family: monospace;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  user-select: all;
}
.key-cell {
  display: flex;
  align-items: center;
  gap: 4px;
}
.key-inline {
  font-family: monospace;
  font-size: 13px;
  user-select: all;
}
.key-hidden {
  color: #909399;
  font-size: 13px;
}
.key-expired {
  color: #e6a23c;
  font-size: 13px;
}
.owner-none {
  color: #909399;
  font-size: 13px;
}
</style>
