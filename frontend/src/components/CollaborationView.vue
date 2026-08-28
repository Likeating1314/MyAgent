<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import IconFontIcon from './IconFontIcon.vue'
import {
  collaborationState,
  createCollaborationRoom,
  openCollaboration,
  stopCollaborationRun,
  submitCollaborationMessage,
} from '../stores/collaboration'
import type { CollaborationAgent } from '../api/client'

const draft = ref('')
const formError = ref('')
const form = reactive({
  title: '方案评审协作室',
  agents: [
    { id: crypto.randomUUID(), name: '协调者', role: '任务协调与综合', prompt: '先拆解问题，最后给出可执行总结。', position: 0, is_coordinator: true },
    { id: crypto.randomUUID(), name: '分析师', role: '技术分析', prompt: '关注证据、约束与实现细节。', position: 1, is_coordinator: false },
  ] as CollaborationAgent[],
})

const agentsById = computed(() => new Map(collaborationState.room?.agents.map(agent => [agent.id, agent]) ?? []))
const timeline = computed(() => {
  const room = collaborationState.room
  if (!room) return []
  const persistedIds = new Set(
    room.events.filter(event => event.event === 'agent_message').map(event => String(event.message_id ?? '')),
  )
  const persisted = room.events
    .filter(event => event.event === 'user_message' || event.event === 'agent_message')
    .map(event => event.event === 'user_message'
      ? { type: 'user' as const, key: `user-${event.sequence}`, sequence: event.sequence, content: String(event.data.content ?? '') }
      : {
          type: 'agent' as const, key: String(event.message_id ?? `agent-${event.sequence}`), sequence: event.sequence,
          message: collaborationState.messages.find(item => item.messageId === String(event.message_id ?? '')),
        })
  const temporary = collaborationState.messages
    .filter(message => !persistedIds.has(message.messageId))
    .map((message, index) => ({ type: 'agent' as const, key: message.messageId, sequence: Number.MAX_SAFE_INTEGER + index, message }))
  return [...persisted, ...temporary].sort((left, right) => left.sequence - right.sequence)
})

function addAgent() {
  if (form.agents.length >= 5) return
  form.agents.push({
    id: crypto.randomUUID(), name: `成员 ${form.agents.length}`,
    role: '专项分析', prompt: '', position: form.agents.length, is_coordinator: false,
  })
}

function removeAgent(index: number) {
  if (form.agents.length <= 2 || form.agents[index]?.is_coordinator) return
  form.agents.splice(index, 1)
  form.agents.forEach((agent, position) => { agent.position = position })
}

function makeCoordinator(index: number) {
  form.agents.forEach((agent, position) => {
    agent.is_coordinator = position === index
  })
}

async function createRoom() {
  formError.value = ''
  if (form.agents.length < 2 || form.agents.length > 5) {
    formError.value = '成员数量必须为 2–5 人。'
    return
  }
  try {
    await createCollaborationRoom({ title: form.title.trim(), agents: form.agents.map(agent => ({ ...agent })) })
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '创建协作房间失败'
  }
}

function send() {
  const message = draft.value.trim()
  if (!message) return
  draft.value = ''
  submitCollaborationMessage(message)
}
</script>

<template>
  <div class="collaboration-layout">
    <aside class="collaboration-sidebar" aria-label="协作房间">
      <div class="collaboration-side-head">
        <div><strong>协作房间</strong><span>{{ collaborationState.rooms.length }} 个</span></div>
      </div>
      <button
        v-for="room in collaborationState.rooms"
        :key="room.id"
        class="collaboration-room-button"
        :class="{ active: collaborationState.room?.id === room.id }"
        type="button"
        :disabled="collaborationState.loading"
        @click="openCollaboration(room.id)"
      >
        <strong>{{ room.title }}</strong>
        <span>{{ room.agent_count }} 位 Agent · {{ room.rounds }} 轮</span>
      </button>

      <form class="collaboration-create" @submit.prevent="createRoom">
        <div class="collaboration-side-head">
          <div><strong>新建协作</strong><span>2–5 位成员</span></div>
        </div>
        <label>房间名称<input v-model="form.title" required maxlength="80" /></label>
        <fieldset>
          <legend>Agent 配置</legend>
          <article v-for="(agent, index) in form.agents" :key="agent.id" class="agent-form-card">
            <div class="agent-form-title">
              <strong>成员 {{ index + 1 }}</strong>
              <button
                v-if="!agent.is_coordinator"
                type="button"
                :aria-label="`移除 ${agent.name || `成员 ${index + 1}`}`"
                :disabled="form.agents.length <= 2"
                @click="removeAgent(index)"
              >移除</button>
            </div>
            <label>名称<input v-model="agent.name" required maxlength="40" /></label>
            <label>角色<input v-model="agent.role" required maxlength="80" /></label>
            <label>角色补充<textarea v-model="agent.prompt" maxlength="4000" rows="2"></textarea></label>
            <label class="coordinator-choice">
              <input type="radio" name="coordinator" :checked="agent.is_coordinator" @change="makeCoordinator(index)" />
              协调者
            </label>
          </article>
        </fieldset>
        <button type="button" :disabled="form.agents.length >= 5" @click="addAgent">添加 Agent</button>
        <button class="button primary" type="submit" :disabled="collaborationState.creating">
          {{ collaborationState.creating ? '创建中…' : '创建房间' }}
        </button>
        <p v-if="formError" role="alert" class="operation-error">{{ formError }}</p>
      </form>
    </aside>

    <main class="collaboration-main">
      <template v-if="collaborationState.room">
        <header class="collaboration-header">
          <div>
            <span class="eyebrow">多 Agent 协作</span>
            <h1>{{ collaborationState.room.title }}</h1>
            <p>固定两轮 · 服务端只读工具 · 刷新可恢复</p>
          </div>
          <span class="context-state" :class="{ running: collaborationState.loading }">
            {{ collaborationState.loading ? '协作运行中' : '协作就绪' }}
          </span>
        </header>

        <div class="collaboration-feed" aria-live="polite">
          <div v-if="!timeline.length" class="collaboration-empty">
            <strong>让 Agent 围绕一个问题展开讨论</strong>
            <span>协调者先拆解，成员依次分析；第二轮互相回应后由协调者汇总。</span>
          </div>
          <template v-for="item in timeline" :key="item.key">
          <article v-if="item.type === 'user'" class="collaboration-user-message">
            <span>你</span><p>{{ item.content }}</p>
          </article>
          <article v-else-if="item.message"
            class="collaboration-agent-message"
            :class="{ temporary: !item.message.persisted }"
          >
            <header>
              <div>
                <strong>{{ item.message.agentName }}</strong>
                <span>{{ item.message.role || agentsById.get(item.message.agentId)?.role }}</span>
              </div>
              <span>第 {{ item.message.round }} 轮 · {{ item.message.persisted ? '已保存' : '生成中' }}</span>
            </header>
            <p>{{ item.message.content }}</p>
          </article>
          </template>
        </div>

        <form class="collaboration-composer" @submit.prevent="send">
          <label class="sr-only" for="collaboration-message">继续协作</label>
          <textarea
            id="collaboration-message" v-model="draft" maxlength="20000" rows="3"
            :disabled="collaborationState.loading" placeholder="提出一个问题，或在同一房间继续追问…"
            @keydown.ctrl.enter.prevent="send"
          ></textarea>
          <div>
            <span>Ctrl + Enter 发送</span>
            <button
              v-if="collaborationState.loading" class="button" type="button"
              aria-label="停止当前协作 run" @click="stopCollaborationRun"
            ><IconFontIcon name="stop" />停止</button>
            <button v-else class="button primary" type="submit" :disabled="!draft.trim()">
              <IconFontIcon name="send" />开始两轮协作
            </button>
          </div>
        </form>
        <p v-if="collaborationState.error" class="operation-error collaboration-feedback" role="alert">{{ collaborationState.error }}</p>
        <p v-if="collaborationState.notice" class="collaboration-feedback" role="status">{{ collaborationState.notice }}</p>
      </template>
      <div v-else class="collaboration-empty room-empty">
        <strong>创建第一个协作房间</strong>
        <span>在左侧配置 2–5 位 Agent，并指定唯一协调者。</span>
      </div>
    </main>

    <aside class="collaboration-members" aria-label="Agent 成员状态">
      <div class="collaboration-side-head"><div><strong>成员</strong><span>名称与角色</span></div></div>
      <article v-for="agent in collaborationState.room?.agents ?? []" :key="agent.id" class="member-card">
        <div><strong>{{ agent.name }}</strong><span v-if="agent.is_coordinator">协调者</span></div>
        <p>{{ agent.role }}</p>
        <span class="member-status">状态：{{ collaborationState.statuses[agent.id] === 'thinking' ? '思考中' : collaborationState.statuses[agent.id] === 'completed' ? '本轮完成' : '等待' }}</span>
      </article>
      <section class="read-only-note">
        <strong>只读安全边界</strong>
        <p>协作模式不提供写文件、运行命令或审批续跑。</p>
      </section>
    </aside>
  </div>
</template>
