<script setup lang="ts">
import type { MessageItem } from '../api/client'

const props = defineProps<{
  message: MessageItem
}>()

function formatContent(value: unknown) {
  if (typeof value === 'string') {
    return value
  }
  return JSON.stringify(value, null, 2)
}

function roleLabel(role: MessageItem['role']) {
  const labels = {
    system: '系统',
    user: '你',
    assistant: '智能体',
    tool: '工具',
  }
  return labels[role]
}
</script>

<template>
  <article class="message" :class="props.message.role">
    <div class="message-role">{{ roleLabel(props.message.role) }}</div>
    <pre class="message-content">{{ formatContent(props.message.content) }}</pre>
  </article>
</template>
