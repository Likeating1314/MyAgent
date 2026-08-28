<script setup lang="ts">
import { ref } from 'vue'
import IconFontIcon from './IconFontIcon.vue'

defineProps<{
  disabled: boolean
}>()

const emit = defineEmits<{
  send: [message: string]
  stop: []
}>()

const input = ref('')

function submit() {
  const value = input.value.trim()
  if (!value) {
    return
  }
  emit('send', value)
  input.value = ''
}
</script>

<template>
  <form class="composer" @submit.prevent="submit">
    <textarea
      v-model="input"
      class="composer-input"
      rows="4"
      aria-label="任务内容"
      placeholder="输入任务，例如：读取说明文件并给我下一步改造计划"
      :disabled="disabled"
      @keydown.ctrl.enter.prevent="submit"
      @keydown.meta.enter.prevent="submit"
    />
    <div class="composer-actions">
      <span class="composer-status">{{ disabled ? '任务运行中，可随时停止' : '准备接收任务' }}</span>
      <button v-if="disabled" class="button stop" type="button" @click="emit('stop')">
        <IconFontIcon class="send-icon" name="stop" />
        <span>停止</span>
      </button>
      <button v-else class="button primary" type="submit">
        <span>发送</span>
        <IconFontIcon class="send-icon" name="send" />
      </button>
    </div>
  </form>
</template>
