<script setup lang="ts">
type IconName = 'chat' | 'collaboration' | 'tools' | 'files' | 'settings' | 'send' | 'stop'

const props = defineProps<{
  name: IconName
}>()

const iconPaths: Record<IconName, string[]> = {
  chat: [
    'M5 6.5A3.5 3.5 0 0 1 8.5 3h7A3.5 3.5 0 0 1 19 6.5v4.8a3.5 3.5 0 0 1-3.5 3.5H11l-4.2 3.1a.8.8 0 0 1-1.28-.64v-2.63A3.5 3.5 0 0 1 3 11.3V6.5Z',
    'M8 8h8M8 11h5.2',
  ],
  collaboration: [
    'M8.2 10.2a3.1 3.1 0 1 0 0-6.2 3.1 3.1 0 0 0 0 6.2ZM15.8 11.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z',
    'M2.8 19.2c.5-4 2.3-6 5.4-6s4.9 2 5.4 6M13 14.2c.8-.5 1.7-.7 2.8-.7 3 0 4.7 1.9 5.2 5.7',
  ],
  tools: [
    'M14.6 4.2a4.4 4.4 0 0 0-5.2 5.2L4.5 14.3a2.2 2.2 0 1 0 3.1 3.1l4.9-4.9a4.4 4.4 0 0 0 5.2-5.2l-2.7 2.7-2.4-2.4 2-3.4Z',
    'M5.9 15.8l.1.1',
  ],
  files: [
    'M3.5 6.2A2.2 2.2 0 0 1 5.7 4h4.1l1.8 2.1h6.7a2.2 2.2 0 0 1 2.2 2.2v8.5a2.2 2.2 0 0 1-2.2 2.2H5.7a2.2 2.2 0 0 1-2.2-2.2V6.2Z',
    'M4 9h16',
  ],
  settings: [
    'M12 8.4a3.6 3.6 0 1 1 0 7.2 3.6 3.6 0 0 1 0-7.2Z',
    'M12 2.8v2.1M12 19.1v2.1M4.9 4.9l1.5 1.5M17.6 17.6l1.5 1.5M2.8 12h2.1M19.1 12h2.1M4.9 19.1l1.5-1.5M17.6 6.4l1.5-1.5',
  ],
  send: [
    'M4 11.6 19.2 4.2a.7.7 0 0 1 .96.84l-4.6 15.18a.7.7 0 0 1-1.22.24l-3.65-4.62-4.84 2.62a.7.7 0 0 1-1.01-.71l.7-4.14L4 11.6Z',
    'm10.69 15.84 3.52-5.3',
    'M5.54 13.61 19.78 4.5',
  ],
  stop: ['M7.2 7.2h9.6v9.6H7.2z'],
}
</script>

<template>
  <svg class="iconfont-symbol" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path
      v-for="path in iconPaths[props.name]"
      :key="path"
      :d="path"
      fill="none"
      stroke="currentColor"
      stroke-width="1.75"
      stroke-linecap="round"
      stroke-linejoin="round"
    />
  </svg>
</template>
