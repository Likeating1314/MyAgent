<script setup lang="ts">
import type { ToolCallRecord } from '../api/client'

defineProps<{
  toolCalls: ToolCallRecord[]
  loading: boolean
}>()

function pretty(value: unknown) {
  if (value == null) {
    return '空'
  }
  if (typeof value === 'string') {
    return value
  }
  return JSON.stringify(translateKeys(value), null, 2)
}

function translateKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(item => translateKeys(item))
  }
  if (!value || typeof value !== 'object') {
    return value
  }
  const keyMap: Record<string, string> = {
    path: '路径',
    content: '内容',
    overwrite: '覆盖',
    overwritten: '已覆盖',
    written_chars: '写入字符数',
    max_chars: '最大字符数',
    total_chars: '总字符数',
    truncated: '已截断',
    root: '根目录',
    entries: '文件项',
    type: '类型',
    directory: '目录',
    file: '文件',
    query: '关键词',
    matches: '匹配项',
    line_number: '行号',
    line: '行内容',
    case_sensitive: '区分大小写',
    max_results: '最大结果数',
    command: '命令',
    cwd: '工作目录',
    timeout_seconds: '超时秒数',
    returncode: '退出码',
    stdout: '标准输出',
    stderr: '错误输出',
    indexed: '已索引',
    skipped: '已跳过',
    limit_reached: '达到上限',
    snippet: '片段',
    score: '分数',
    total_matches: '总匹配数',
    subcommand: '子命令',
    args: '参数',
    updated_at: '更新时间',
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [keyMap[key] ?? key, translateKeys(item)]),
  )
}

function summary(call: ToolCallRecord) {
  if (call.status === 'error') {
    return typeof call.error === 'string' ? call.error : '执行失败，展开查看详情'
  }
  const result = call.result as Record<string, unknown> | undefined
  if (!result || typeof result !== 'object') {
    return '已完成'
  }
  if (Array.isArray(result.entries)) {
    return `${result.entries.length} 项文件记录`
  }
  if (Array.isArray(result.matches)) {
    if (typeof result.total_matches === 'number') {
      return `${result.matches.length} 条知识库结果`
    }
    return `${result.matches.length} 处文本匹配`
  }
  if (typeof result.path === 'string' && typeof result.total_chars === 'number') {
    return `${result.path} · ${result.total_chars} 字符`
  }
  if (typeof result.written_chars === 'number') {
    return `写入 ${result.written_chars} 字符`
  }
  if (typeof result.returncode === 'number') {
    return `退出码 ${result.returncode}`
  }
  if (typeof result.indexed === 'number') {
    return `索引 ${result.indexed} 个文件`
  }
  return '已完成'
}

function statusLabel(status: ToolCallRecord['status']) {
  return status === 'ok' ? '成功' : '失败'
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
    git_inspect: 'Git 检查',
  }
  return labels[name] ?? name
}
</script>

<template>
  <div class="tool-timeline" :class="{ running: loading }">
    <div v-if="!toolCalls.length" class="empty-inline">暂无工具调用</div>
    <article v-for="(call, index) in toolCalls" :key="index" class="tool-step">
      <div class="step-marker" :class="call.status">
        <span aria-hidden="true">{{ call.status === 'ok' ? '✓' : '!' }}</span>
      </div>
      <div class="tool-call-head">
        <div class="tool-copy">
          <strong>{{ toolNameLabel(call.name) }}</strong>
          <span>{{ summary(call) }}</span>
        </div>
        <span class="tool-status" :class="call.status">{{ statusLabel(call.status) }}</span>
        <details class="tool-details">
          <summary>详情</summary>
          <div class="tool-call-block">
            <div class="tool-call-label">参数</div>
            <pre>{{ pretty(call.arguments) }}</pre>
          </div>
          <div class="tool-call-block">
            <div class="tool-call-label">结果</div>
            <pre>{{ pretty(call.status === 'ok' ? call.result : call.error) }}</pre>
          </div>
        </details>
      </div>
    </article>
  </div>
</template>
