<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AgentSettings } from '../api/client'

const props = defineProps<{
  modelValue: AgentSettings
  credentialNotice?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: AgentSettings]
}>()

const showSecret = ref(false)

const providerOptions = [
  {
    value: 'openai',
    label: 'OpenAI',
    apiBaseUrl: 'https://api.openai.com/v1',
    model: 'gpt-4.1-mini',
  },
  {
    value: 'deepseek',
    label: '深度求索',
    apiBaseUrl: 'https://api.deepseek.com/v1',
    model: 'deepseek-chat',
  },
  {
    value: 'qwen',
    label: '通义千问',
    apiBaseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    model: 'qwen-plus',
  },
  {
    value: 'siliconflow',
    label: '硅基流动',
    apiBaseUrl: 'https://api.siliconflow.cn/v1',
    model: 'Qwen/Qwen2.5-72B-Instruct',
  },
  {
    value: 'custom',
    label: '自定义',
    apiBaseUrl: '',
    model: '',
  },
] as const

const apiProvider = computed({
  get: () => props.modelValue.api_provider,
  set: value => {
    const preset = providerOptions.find(item => item.value === value)
    emit('update:modelValue', {
      ...props.modelValue,
      api_provider: value,
      api_base_url: preset?.apiBaseUrl || props.modelValue.api_base_url,
      model: preset?.model || props.modelValue.model,
    })
  },
})

const apiKey = computed({
  get: () => props.modelValue.api_key,
  set: value => emit('update:modelValue', { ...props.modelValue, api_key: value }),
})

const model = computed({
  get: () => props.modelValue.model,
  set: value => emit('update:modelValue', { ...props.modelValue, model: value }),
})

const apiBaseUrl = computed({
  get: () => props.modelValue.api_base_url,
  set: value => emit('update:modelValue', { ...props.modelValue, api_base_url: value }),
})

const allowCommandExecution = computed({
  get: () => props.modelValue.allow_command_execution,
  set: value => emit('update:modelValue', { ...props.modelValue, allow_command_execution: value }),
})

const maxAgentSteps = computed({
  get: () => props.modelValue.max_agent_steps,
  set: value => emit('update:modelValue', { ...props.modelValue, max_agent_steps: value }),
})
</script>

<template>
  <form class="settings-grid" @submit.prevent>
    <label class="field">
      <span>模型服务商</span>
      <select v-model="apiProvider">
        <option v-for="provider in providerOptions" :key="provider.value" :value="provider.value">
          {{ provider.label }}
        </option>
      </select>
    </label>
    <label class="field">
      <span>接口密钥</span>
      <div class="secret-input-wrap">
        <input
          v-model="apiKey"
          :type="showSecret ? 'text' : 'password'"
          autocomplete="off"
          placeholder="未填写时使用本地演示模式"
        />
        <button type="button" @click="showSecret = !showSecret">
          {{ showSecret ? '隐藏' : '显示' }}
        </button>
      </div>
      <small>{{ credentialNotice || 'Web 模式下密钥仅保存在当前页面内存，不写入会话数据库。' }}</small>
    </label>
    <label class="field">
      <span>模型</span>
      <input v-model="model" type="text" autocomplete="off" />
    </label>
    <label class="field">
      <span>接口地址</span>
      <input v-model="apiBaseUrl" type="url" autocomplete="off" />
    </label>
    <label class="field checkbox">
      <input v-model="allowCommandExecution" type="checkbox" />
      <span>允许执行命令</span>
    </label>
    <label class="field">
      <span>最大步数</span>
      <input v-model.number="maxAgentSteps" type="number" min="1" max="32" inputmode="numeric" />
    </label>
  </form>
</template>
