<script setup lang="ts">
import { computed } from 'vue'
import MessageItem from './MessageItem.vue'
import type { MessageItem as ChatMessage } from '../api/client'

const props = defineProps<{
  messages: ChatMessage[]
  loading: boolean
}>()

const emit = defineEmits<{
  send: [message: string]
}>()

const quickPrompts = [
  '读取说明文件，总结这个项目能做什么',
  '检查前后端接口是否一致',
  '给我一份下一步开发计划',
]

function messageText(message: ChatMessage) {
  if (typeof message.content === 'string') {
    return message.content
  }
  return JSON.stringify(message.content ?? '')
}

const visibleMessages = computed(() =>
  props.messages.filter(message => message.role !== 'assistant' || messageText(message).trim().length > 0),
)
</script>

<template>
  <div class="message-list">
    <MessageItem v-for="(message, index) in visibleMessages" :key="index" :message="message" />
    <article v-if="loading" class="message assistant loading-message">
      <div class="message-role">智能体</div>
      <div class="thinking-line">
        <span class="dotm-loader" role="status" aria-label="加载中">
          <span class="dotm-grid" aria-hidden="true">
            <span class="dotm-dot" style="--dmx-path: 0.5"></span>
            <span class="dotm-dot" style="--dmx-path: 0.75"></span>
            <span class="dotm-dot" style="--dmx-path: 1"></span>
            <span class="dotm-dot" style="--dmx-path: 0.25"></span>
            <span class="dotm-dot" style="--dmx-path: 0.5"></span>
            <span class="dotm-dot" style="--dmx-path: 0.75"></span>
            <span class="dotm-dot" style="--dmx-path: 0"></span>
            <span class="dotm-dot" style="--dmx-path: 0.25"></span>
            <span class="dotm-dot" style="--dmx-path: 0.5"></span>
          </span>
        </span>
        <div>
          <div class="thinking-copy">智能体正在理解任务并整理下一步</div>
          <div class="thread-line" aria-hidden="true"><div class="thread-fill"></div></div>
        </div>
      </div>
    </article>
    <div v-if="!visibleMessages.length && !loading" class="empty-state">
      <strong>开始一个本地任务</strong>
      <span>选择一个任务，或直接输入你的指令。</span>
      <div class="prompt-grid" aria-label="快捷任务">
        <button v-for="prompt in quickPrompts" :key="prompt" type="button" @click="emit('send', prompt)">
          {{ prompt }}
        </button>
      </div>
    </div>
  </div>
</template>
