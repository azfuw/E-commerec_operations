<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { login } from '../api'
import { canAccessDepartment, homeLocation } from '../capabilities'
import { logisticsLocation } from '../logistics'
import { session } from '../session'
import InlineError from '../components/InlineError.vue'

const router = useRouter()
const route = useRoute()
const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref('')

async function submit(): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  errorMessage.value = ''
  try {
    await login(username.value, password.value)
    if (!session.user) throw new Error('missing current user')
    await router.replace(
      route.query.next === 'logistics' && canAccessDepartment(session.user.role, session.user.department, 'logistics')
        ? logisticsLocation(route.query.view)
        : homeLocation(session.user),
    )
  } catch {
    errorMessage.value = '登录失败，请检查账号或密码'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-panel" aria-labelledby="login-title">
      <div class="login-heading">
        <p class="eyebrow">TEAM WORKSPACE</p>
        <h1 id="login-title">登录智营台</h1>
        <p class="muted">使用团队账号进入运营或物流工作台。</p>
      </div>
      <form class="login-form" @submit.prevent="submit">
        <div class="form-field">
          <label for="username">用户名</label>
          <el-input
            id="username"
            v-model="username"
            name="username"
            autocomplete="username"
            :disabled="submitting"
          />
        </div>
        <div class="form-field">
          <label for="password">密码</label>
          <el-input
            id="password"
            v-model="password"
            name="password"
            type="password"
            autocomplete="current-password"
            show-password
            :disabled="submitting"
          />
        </div>
        <InlineError v-if="errorMessage" :message="errorMessage" />
        <el-button
          native-type="submit"
          type="primary"
          :loading="submitting"
          :disabled="submitting || !username || !password"
        >
          登录
        </el-button>
      </form>
    </section>
  </main>
</template>
