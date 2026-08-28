<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AuthGate from './components/AuthGate.vue'
import ChatWindow from './components/ChatWindow.vue'
import CollaborationView from './components/CollaborationView.vue'
import IconFontIcon from './components/IconFontIcon.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import ToolCallPanel from './components/ToolCallPanel.vue'
import {
  approveCommand,
  approvalDisplayState,
  archiveSession,
  chatState,
  clearUserData,
  initializeChat,
  rejectCommand,
  renameSession,
  restoreArchivedSession,
  startNewSession,
  stopCurrentRequest,
  submitMessage,
  switchSession,
  updateSettings,
} from './stores/chat'
import { isApprovalActionable } from './stores/sessionLifecycle'
import { clearCollaborationData, collaborationState, initializeCollaborations } from './stores/collaboration'
import { authState, initializeAuth, logout } from './stores/auth'

const settingsModel = computed({
  get: () => chatState.settings,
  set: value => updateSettings(value),
})

type NavKey = 'chat' | 'collaboration' | 'tools' | 'files' | 'settings'
type NavIcon = 'chat' | 'collaboration' | 'tools' | 'files' | 'settings'

const activeNav = ref<NavKey>('chat')
const hoveredNavLabel = ref('')
const editingSessionId = ref('')
const sessionTitleDraft = ref('')
const showArchivedSessions = ref(false)
const navTooltipLeft = ref(0)
const navTooltipTop = ref(0)
const navTooltipPlacement = ref<'right' | 'bottom'>('right')
const navItems: Array<{ key: NavKey; label: string; icon: NavIcon }> = [
  { key: 'chat', label: '对话', icon: 'chat' },
  { key: 'collaboration', label: '协作', icon: 'collaboration' },
  { key: 'tools', label: '工具市场', icon: 'tools' },
  { key: 'files', label: '文件', icon: 'files' },
  { key: 'settings', label: '设置', icon: 'settings' },
]

const completedToolCalls = computed(() => chatState.toolCalls.filter(call => call.status === 'ok').length)
const failedToolCalls = computed(() => chatState.toolCalls.filter(call => call.status === 'error').length)
const commandModeLabel = computed(() =>
  chatState.runtime?.command_execution_allowed && chatState.settings.allow_command_execution
    ? '已开启'
    : '未开启',
)
const toolCountLabel = computed(() => `${chatState.tools.length} 个工具`)
const leftPanelOpen = computed(() => !['chat', 'collaboration'].includes(activeNav.value))
const visibleApprovals = computed(() =>
  chatState.approvals.filter(approval => approval.session_id === chatState.sessionId),
)
const activeRailIndex = computed(() => Math.max(navItems.findIndex(item => item.key === activeNav.value), 0))

function formatTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function toolNameLabel(name: string) {
  const labels: Record<string, string> = {
    read_file: '读取文件',
    write_file: '写入文件',
    list_files: '列出文件',
    search_text: '搜索文本',
    run_command: '运行命令',
    index_workspace: '索引知识库',
    query_knowledge: '查询知识库',
    git_inspect: '代码仓库检查',
  }
  return labels[name] ?? name
}

function openSession(sessionId: string) {
  switchSession(sessionId).catch(error => {
    chatState.error = error instanceof Error ? error.message : '切换会话失败'
  })
}

function createFreshSession() {
  startNewSession().catch(error => {
    chatState.error = error instanceof Error ? error.message : '新建会话失败'
  })
}

function approvePending(approvalId: string) {
  approveCommand(approvalId).catch(error => {
    chatState.error = error instanceof Error ? error.message : '审批失败'
  })
}

function beginRename(session: { session_id: string; display_title: string }) {
  editingSessionId.value = session.session_id
  sessionTitleDraft.value = session.display_title
}

function cancelRename() {
  editingSessionId.value = ''
  sessionTitleDraft.value = ''
}

function commitRename(sessionId: string) {
  renameSession(sessionId, sessionTitleDraft.value)
    .then(completed => {
      if (completed) cancelRename()
    })
    .catch(() => undefined)
}

function archiveSelected(sessionId: string) {
  archiveSession(sessionId).catch(() => undefined)
}

function restoreSelected(sessionId: string) {
  restoreArchivedSession(sessionId).catch(() => undefined)
}

function approvalStateLabel(approval: typeof chatState.approvals[number]) {
  if (approval.replacement_approval_id) return '需重新审批'
  const labels = {
    waiting: approval.status === 'approved' ? '已批准，等待继续' : '等待审批',
    running: '执行中',
    completed: '已完成',
    reapproval_required: '工具未成功',
    rejected: '已拒绝',
    expired: '已过期',
    uncertain: '结果不确定',
    cancelled: '已取消',
  }
  return labels[approvalDisplayState(approval)]
}

function approvalCanRun(approval: typeof chatState.approvals[number]) {
  return isApprovalActionable(approval, approvalDisplayState(approval))
}

function replacementApproval(approval: typeof chatState.approvals[number]) {
  return approval.replacement_approval_id
    ? chatState.approvals.find(item => item.id === approval.replacement_approval_id)
    : undefined
}

function rejectPending(approvalId: string) {
  rejectCommand(approvalId).catch(() => undefined)
}

function selectNav(key: NavKey) {
  activeNav.value = key
  if (key === 'collaboration') {
    initializeCollaborations().catch(error => {
      collaborationState.error = error instanceof Error ? error.message : '协作初始化失败'
    })
  }
  hideNavTooltip()
}

function showNavTooltip(label: string, event: MouseEvent | FocusEvent) {
  const target = event.currentTarget
  if (!(target instanceof HTMLElement)) {
    return
  }
  const rect = target.getBoundingClientRect()
  hoveredNavLabel.value = label
  if (window.innerWidth <= 900) {
    navTooltipPlacement.value = 'bottom'
    navTooltipLeft.value = rect.left + rect.width / 2
    navTooltipTop.value = rect.bottom + 10
    return
  }
  navTooltipPlacement.value = 'right'
  navTooltipLeft.value = rect.right + 12
  navTooltipTop.value = rect.top + rect.height / 2
}

function hideNavTooltip() {
  hoveredNavLabel.value = ''
}

async function logoutUser() {
  stopCurrentRequest()
  clearCollaborationData()
  clearUserData()
  await logout()
}

onMounted(async () => {
  await initializeAuth()
  if (authState.authenticated) {
    initializeChat().catch(error => {
      chatState.error = error instanceof Error ? error.message : '初始化失败'
    })
  }
})
</script>

<template>
  <div v-if="!authState.ready" class="auth-loading" role="status">正在恢复登录状态…</div>
  <AuthGate v-else-if="!authState.authenticated" @authenticated="initializeChat()" />
  <div v-else class="desktop-shell" :class="{ 'is-running': chatState.loading }">
    <div class="sr-only" aria-live="polite" aria-atomic="true">{{ chatState.notice }}</div>
    <div class="sr-only" role="alert">{{ chatState.error }}</div>
    <header class="window-bar">
      <div class="traffic" aria-hidden="true">
        <span class="traffic-dot close"></span>
        <span class="traffic-dot minimize"></span>
        <span class="traffic-dot zoom"></span>
      </div>
      <div class="window-title">
        <strong>MyAgent</strong>
        <span>本地工作区</span>
      </div>
      <div class="window-meta"><span>{{ authState.user?.displayName }}</span><button class="user-logout" type="button" @click="logoutUser">退出</button></div>
    </header>

    <div class="app-shell">
      <section
        class="left-navigation"
        :class="{ 'panel-open': leftPanelOpen }"
        :style="{ '--active-rail-index': activeRailIndex }"
      >
        <nav class="rail" aria-label="应用导航">
          <div class="rail-stack">
            <span class="rail-glider" aria-hidden="true"></span>
            <button
              v-for="item in navItems"
              :key="item.key"
              class="rail-button"
              :class="{ active: activeNav === item.key }"
              type="button"
              :aria-label="item.label"
              :aria-pressed="activeNav === item.key"
              @mouseenter="showNavTooltip(item.label, $event)"
              @mouseleave="hideNavTooltip"
              @focus="showNavTooltip(item.label, $event)"
              @blur="hideNavTooltip"
              @click="selectNav(item.key)"
            >
              <IconFontIcon class="rail-icon" :name="item.icon" />
            </button>
          </div>
        </nav>

        <aside class="rail-drawer" :aria-hidden="!leftPanelOpen">
          <Transition name="nav-slide" mode="out-in">
            <section v-if="activeNav === 'tools'" key="tools" class="rail-panel">
              <div class="rail-panel-head">
                <strong>工具市场</strong>
                <span>{{ toolCountLabel }}</span>
              </div>
              <div class="tool-market nav-tool-market">
                <article v-for="tool in chatState.tools" :key="tool.name" class="tool-market-row">
                  <strong>{{ toolNameLabel(tool.name) }}</strong>
                  <span>{{ tool.description }}</span>
                </article>
                <div v-if="!chatState.tools.length" class="empty-inline">暂无可用工具</div>
              </div>
            </section>

            <section v-else-if="activeNav === 'settings'" key="settings" class="rail-panel">
              <div class="rail-panel-head">
                <strong>设置</strong>
                <span>{{ chatState.settings.max_agent_steps }} 步上限</span>
              </div>
              <SettingsPanel v-model="settingsModel" :credential-notice="chatState.credentialNotice" />
            </section>

            <section v-else-if="activeNav === 'files'" key="files" class="rail-panel">
              <div class="rail-panel-head">
                <strong>文件</strong>
                <span>工作区</span>
              </div>
              <div class="file-panel">
                <span>{{ chatState.runtime?.workspace || '正在读取工作区…' }}</span>
                <button type="button" @click="submitMessage('列出工作区文件')">列出文件</button>
                <button type="button" @click="submitMessage('搜索项目中的 TODO')">搜索待办</button>
              </div>
            </section>
          </Transition>
        </aside>
      </section>

      <main v-if="activeNav !== 'collaboration'" class="main">
        <ChatWindow
          :messages="chatState.messages"
          :loading="chatState.loading"
          :error="chatState.error"
          :notice="chatState.notice"
          :session-id="chatState.sessionId"
          :workspace="chatState.runtime?.workspace || ''"
          @send="submitMessage"
          @stop="stopCurrentRequest"
        />
      </main>

      <CollaborationView v-else class="collaboration-page" />

      <aside v-if="activeNav !== 'collaboration'" class="context-pane">
        <section class="context-section overview-section" aria-label="运行概览">
          <div class="section-head">
            <div>
              <strong>运行概览</strong>
              <span>{{ chatState.settings.model }}</span>
            </div>
            <span class="context-state" :class="{ running: chatState.loading }">
              {{ chatState.loading ? '运行中' : '就绪' }}
            </span>
          </div>
          <div class="overview-grid">
            <div class="metric">
              <span>消息</span>
              <strong>{{ chatState.messages.length }}</strong>
            </div>
            <div class="metric">
              <span>工具</span>
              <strong>{{ completedToolCalls }}/{{ chatState.toolCalls.length }}</strong>
            </div>
            <div class="metric">
              <span>失败</span>
              <strong>{{ failedToolCalls }}</strong>
            </div>
            <div class="metric">
              <span>命令</span>
              <strong>{{ commandModeLabel }}</strong>
            </div>
          </div>
        </section>

        <section class="context-section compact-section">
          <div class="section-head">
            <div>
              <strong>会话</strong>
              <span>{{ chatState.sessions.length }} 个活跃会话</span>
            </div>
            <button class="small-button" type="button" :disabled="chatState.loading" @click="createFreshSession">
              新建
            </button>
          </div>
          <div class="session-list">
            <article
              v-for="session in chatState.sessions"
              :key="session.session_id"
              class="session-entry"
              :class="{ active: session.session_id === chatState.sessionId }"
              :aria-busy="Boolean(chatState.sessionOperations[session.session_id])"
            >
              <form
                v-if="editingSessionId === session.session_id"
                class="session-rename"
                @submit.prevent="commitRename(session.session_id)"
              >
                <label class="sr-only" :for="`session-title-${session.session_id}`">会话标题</label>
                <input
                  :id="`session-title-${session.session_id}`"
                  v-model="sessionTitleDraft"
                  maxlength="80"
                  required
                  autofocus
                  :disabled="chatState.sessionOperations[session.session_id] === 'rename'"
                />
                <div class="session-actions">
                  <button
                    type="submit"
                    :disabled="chatState.sessionOperations[session.session_id] === 'rename'"
                  >{{ chatState.sessionOperations[session.session_id] === 'rename' ? '保存中…' : '保存' }}</button>
                  <button
                    type="button"
                    :disabled="chatState.sessionOperations[session.session_id] === 'rename'"
                    @click="cancelRename"
                  >取消</button>
                </div>
              </form>
              <template v-else>
                <button
                  class="session-row"
                  type="button"
                  :disabled="chatState.loading"
                  @click="openSession(session.session_id)"
                >
                  <span>{{ session.display_title }}</span>
                  <small>{{ formatTime(session.updated_at) }} · {{ session.message_count }} 条消息</small>
                </button>
                <div class="session-actions">
                  <button
                    type="button"
                    :disabled="Boolean(chatState.sessionOperations[session.session_id])"
                    @click="beginRename(session)"
                  >重命名</button>
                  <button
                    type="button"
                    :disabled="Boolean(chatState.sessionOperations[session.session_id])"
                    @click="archiveSelected(session.session_id)"
                  >{{ chatState.sessionOperations[session.session_id] === 'archive' ? '归档中…' : '归档' }}</button>
                </div>
              </template>
              <p v-if="chatState.sessionOperationErrors[session.session_id]" class="operation-error" role="alert">
                {{ chatState.sessionOperationErrors[session.session_id] }}
              </p>
            </article>
            <div v-if="!chatState.sessions.length" class="empty-inline">暂无持久化会话</div>
          </div>
          <button
            class="archive-toggle"
            type="button"
            :aria-expanded="showArchivedSessions"
            @click="showArchivedSessions = !showArchivedSessions"
          >
            {{ showArchivedSessions ? '收起归档' : `归档会话（${chatState.archivedSessions.length}）` }}
          </button>
          <div v-if="showArchivedSessions" class="session-list archived-list">
            <article
              v-for="session in chatState.archivedSessions"
              :key="session.session_id"
              class="session-entry"
              :aria-busy="Boolean(chatState.sessionOperations[session.session_id])"
            >
              <div class="session-row is-readonly">
                <span>{{ session.display_title }}</span>
                <small>{{ session.archived_at ? formatTime(session.archived_at) : '' }}</small>
              </div>
              <div class="session-actions">
                <button
                  type="button"
                  :disabled="Boolean(chatState.sessionOperations[session.session_id])"
                  @click="restoreSelected(session.session_id)"
                >{{ chatState.sessionOperations[session.session_id] === 'unarchive' ? '恢复中…' : '恢复' }}</button>
              </div>
              <p v-if="chatState.sessionOperationErrors[session.session_id]" class="operation-error" role="alert">
                {{ chatState.sessionOperationErrors[session.session_id] }}
              </p>
            </article>
            <div v-if="!chatState.archivedSessions.length" class="empty-inline">暂无归档会话</div>
          </div>
        </section>

        <section class="context-section">
          <div class="section-head">
            <div>
              <strong>工具轨迹</strong>
              <span>{{ chatState.toolCalls.length }} 步</span>
            </div>
            <span class="context-state" :class="{ running: chatState.loading }">
              {{ chatState.loading ? '运行中' : '就绪' }}
            </span>
          </div>
          <ToolCallPanel :tool-calls="chatState.toolCalls" :loading="chatState.loading" />
        </section>

        <section class="context-section compact-section">
          <div class="section-head">
            <div>
              <strong>审批</strong>
              <span>{{ visibleApprovals.length }} 条记录</span>
            </div>
          </div>
          <div class="approval-list">
            <article
              v-for="approval in visibleApprovals"
              :key="approval.id"
              :id="`approval-${approval.id}`"
              class="approval-card"
              :aria-busy="chatState.activeApprovalId === approval.id || Boolean(chatState.approvalOperations[approval.id])"
            >
              <div class="approval-title">
                <strong>{{ toolNameLabel(approval.tool_name) }}</strong>
                <span class="approval-state" :class="approvalDisplayState(approval)">{{ approvalStateLabel(approval) }}</span>
              </div>
              <span>{{ approval.reason }}</span>
              <span v-if="typeof approval.details.path === 'string'" class="approval-path">
                {{ approval.details.change_type === 'overwrite' ? '覆盖' : '新建' }} ·
                {{ approval.details.path }}
              </span>
              <details v-if="typeof approval.details.diff === 'string'" class="approval-diff">
                <summary>查看变更 diff</summary>
                <pre><code>{{ approval.details.diff }}</code></pre>
              </details>
              <p v-if="approval.replacement_approval_id" class="approval-replacement">
                原审批已失效；请使用
                <a :href="`#approval-${approval.replacement_approval_id}`">
                  {{ replacementApproval(approval)?.reason || '替代审批' }}
                </a>
                重新确认。
              </p>
              <div class="approval-actions">
                <button
                  v-if="approvalCanRun(approval)"
                  type="button"
                  :disabled="chatState.loading || Boolean(chatState.approvalOperations[approval.id])"
                  @click="approvePending(approval.id)"
                >{{ approvalDisplayState(approval) === 'reapproval_required' ? '重新尝试' : '批准并继续' }}</button>
                <button
                  v-if="approval.status === 'pending' && approvalDisplayState(approval) === 'waiting'"
                  type="button"
                  :disabled="chatState.loading || Boolean(chatState.approvalOperations[approval.id])"
                  @click="rejectPending(approval.id)"
                >{{ chatState.approvalOperations[approval.id] === 'reject' ? '拒绝中…' : '拒绝' }}</button>
              </div>
              <p v-if="chatState.approvalOperationErrors[approval.id]" class="operation-error" role="alert">
                {{ chatState.approvalOperationErrors[approval.id] }}
              </p>
            </article>
            <div v-if="!visibleApprovals.length" class="empty-inline">暂无审批记录</div>
          </div>
        </section>

      </aside>
    </div>

    <div
      v-if="hoveredNavLabel"
      class="floating-rail-tooltip"
      :class="navTooltipPlacement"
      :style="{ left: `${navTooltipLeft}px`, top: `${navTooltipTop}px` }"
      role="tooltip"
    >
      {{ hoveredNavLabel }}
    </div>
  </div>
</template>
