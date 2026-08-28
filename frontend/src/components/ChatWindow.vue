<script setup lang="ts">
import Composer from './Composer.vue'
import MessageList from './MessageList.vue'
import type { MessageItem } from '../api/client'

defineProps<{
  messages: MessageItem[]
  loading: boolean
  error: string
  notice: string
  sessionId: string
  workspace: string
}>()

defineEmits<{
  send: [message: string]
  stop: []
}>()
</script>

<template>
  <section class="chat-window">
    <header class="chat-header">
      <div class="chat-heading">
        <div class="chat-kicker">MyAgent 工作台</div>
        <div class="chat-title">MyAgent</div>
        <div class="chat-subtitle">
          <span>工作区 {{ workspace || '正在读取…' }}</span>
          <span class="session-chip">会话 {{ sessionId }}</span>
        </div>
      </div>
      <div class="chat-status" :class="{ loading }">
        <span class="status-light" aria-hidden="true"></span>
        <span>{{ loading ? '智能体运行中' : '空闲' }}</span>
      </div>
    </header>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-else-if="notice" class="notice-banner">{{ notice }}</div>

    <MessageList :messages="messages" :loading="loading" @send="$emit('send', $event)" />

    <Composer :disabled="loading" @send="$emit('send', $event)" @stop="$emit('stop')" />
  </section>
</template>
