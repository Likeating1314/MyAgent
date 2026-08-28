import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import type { ApprovalRequest, SessionSummary, ToolCallRecord } from '../src/api/client.ts'
import {
  approvalResultNotice,
  chooseSessionAfterArchive,
  isApprovalActionable,
  resolveApprovalState,
  runOperationOnce,
} from '../src/stores/sessionLifecycle.ts'

const approval: ApprovalRequest = {
  id: 'approval-1',
  session_id: 'session-1',
  tool_name: 'write_file',
  arguments: { path: 'file.txt' },
  reason: 'write',
  details: {},
  status: 'approved',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  expires_at: '2099-01-01T01:00:00Z',
}

function summary(sessionId: string): SessionSummary {
  return {
    session_id: sessionId,
    display_title: sessionId,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    message_count: 0,
    tool_call_count: 0,
  }
}

test('approval state is derived from refreshed approval facts and the latest matching tool record', () => {
  assert.equal(
    resolveApprovalState(approval, [], { activeApprovalId: approval.id, loading: true, now: 0 }),
    'running',
  )
  assert.equal(resolveApprovalState(approval, []), 'waiting')
  assert.equal(
    resolveApprovalState({ ...approval, expires_at: '2020-01-01T00:00:00Z' }, []),
    'expired',
  )

  const consumed = { ...approval, status: 'consumed' as const }
  const matchingRecord: ToolCallRecord = {
    name: 'write_file',
    arguments: { approval_id: approval.id },
    status: 'ok',
    result: {},
  }
  assert.equal(resolveApprovalState(consumed, [matchingRecord]), 'completed')
  assert.equal(resolveApprovalState(consumed, []), 'uncertain')
  const errorRecord: ToolCallRecord = { ...matchingRecord, status: 'error', error: 'failed' }
  assert.equal(resolveApprovalState(consumed, [matchingRecord, errorRecord]), 'uncertain')
  assert.equal(resolveApprovalState(approval, [errorRecord]), 'reapproval_required')
  assert.equal(resolveApprovalState({ ...approval, status: 'pending' }, [errorRecord]), 'reapproval_required')
  assert.equal(resolveApprovalState({ ...approval, last_resume_outcome: 'cancelled' }, []), 'cancelled')
  assert.equal(resolveApprovalState({ ...approval, status: 'rejected' }, [matchingRecord]), 'rejected')
  assert.equal(
    resolveApprovalState(
      { ...approval, status: 'rejected', expires_at: '2020-01-01T00:00:00Z', last_resume_outcome: 'cancelled' },
      [errorRecord],
    ),
    'rejected',
  )
  assert.equal(
    resolveApprovalState({ ...consumed, last_resume_outcome: 'cancelled' }, [matchingRecord]),
    'completed',
  )
  assert.equal(
    resolveApprovalState({ ...consumed, last_resume_outcome: 'cancelled' }, [errorRecord]),
    'uncertain',
  )
  assert.equal(
    resolveApprovalState({ ...consumed, last_resume_outcome: 'cancelled' }, []),
    'uncertain',
  )
  assert.equal(
    resolveApprovalState(
      { ...approval, expires_at: '2020-01-01T00:00:00Z', last_resume_outcome: 'cancelled' },
      [errorRecord],
    ),
    'expired',
  )
  assert.equal(
    resolveApprovalState({ ...approval, last_resume_outcome: 'cancelled' }, [errorRecord]),
    'reapproval_required',
  )

  const refreshedApproval = structuredClone(consumed)
  const refreshedRecords = structuredClone([matchingRecord])
  assert.equal(
    resolveApprovalState(refreshedApproval, refreshedRecords),
    resolveApprovalState(consumed, [matchingRecord]),
  )
})

test('replacement approval is the only actionable entry after a stale failure', () => {
  const stale = {
    ...approval,
    replacement_approval_id: 'approval-2',
  }
  const replacement = {
    ...approval,
    id: 'approval-2',
    status: 'pending' as const,
  }
  const staleError: ToolCallRecord = {
    name: 'write_file',
    arguments: { approval_id: stale.id },
    status: 'error',
    result: {},
    error: 'target changed',
  }
  const staleState = resolveApprovalState(stale, [staleError])
  const replacementState = resolveApprovalState(replacement, [staleError])

  assert.equal(staleState, 'reapproval_required')
  assert.equal(isApprovalActionable(stale, staleState), false)
  assert.equal(replacementState, 'waiting')
  assert.equal(isApprovalActionable(replacement, replacementState), true)
  assert.equal(
    isApprovalActionable({ ...approval, last_resume_outcome: 'cancelled' }, 'cancelled'),
    true,
  )
  assert.equal(isApprovalActionable({ ...approval, status: 'consumed' }, 'uncertain'), false)
})

test('done describes agent termination separately from the approved tool result', () => {
  assert.equal(approvalResultNotice('completed', 'done'), '工具已执行完成，续跑正常结束。')
  assert.equal(
    approvalResultNotice('reapproval_required', 'done'),
    '续跑已结束，但获批工具未成功，请查看工具记录。',
  )
  assert.equal(approvalResultNotice('uncertain', 'done'), '结果不确定，不会自动重试。')
})

test('rename, archive, unarchive and reject coalesce double clicks per resource', async () => {
  for (const operation of ['rename', 'archive', 'unarchive', 'reject'] as const) {
    const active: Record<string, typeof operation | undefined> = {}
    const errors: Record<string, string | undefined> = {}
    let calls = 0
    let release!: () => void
    const gate = new Promise<void>(resolve => {
      release = resolve
    })
    const action = async () => {
      calls += 1
      await gate
    }
    const first = runOperationOnce(active, errors, 'resource', operation, action, message => message)
    const duplicate = await runOperationOnce(
      active,
      errors,
      'resource',
      operation,
      action,
      message => message,
    )
    assert.equal(duplicate, false)
    assert.equal(active.resource, operation)
    assert.equal(calls, 1)
    release()
    assert.equal(await first, true)
    assert.equal(active.resource, undefined)
  }
})

test('operation failure clears pending state and exposes actionable recovery guidance', async () => {
  const active: Record<string, 'rename' | undefined> = {}
  const errors: Record<string, string | undefined> = {}
  await assert.rejects(
    runOperationOnce(
      active,
      errors,
      'session',
      'rename',
      async () => {
        throw new Error('标题无效。')
      },
      message => `${message} 请修正标题后重试。`,
    ),
  )
  assert.equal(active.session, undefined)
  assert.equal(errors.session, '标题无效。 请修正标题后重试。')
})

test('session and approval rows expose operation busy and inline alert semantics', () => {
  const source = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  assert.match(source, /:aria-busy="Boolean\(chatState\.sessionOperations/)
  assert.match(source, /chatState\.approvalOperations\[approval\.id\]/)
  assert.match(source, /class="operation-error" role="alert"/)
  assert.match(source, /:disabled="Boolean\(chatState\.sessionOperations/)
  assert.match(source, /:id="`approval-\$\{approval\.id\}`"/)
  assert.match(source, /approval\.replacement_approval_id/)
  assert.match(source, /v-if="approvalCanRun\(approval\)"/)
  assert.match(source, /原审批已失效/)
})

test('approval terminal failures always release loading and transient operation state', () => {
  const source = readFileSync(new URL('../src/stores/chat.ts', import.meta.url), 'utf8')
  const approvalFlow = source.slice(
    source.indexOf('export async function approveCommand'),
    source.indexOf('export async function rejectCommand'),
  )
  assert.match(approvalFlow, /finally\s*{/)
  assert.match(approvalFlow, /requestLifecycle\.finish\(controller\)/)
  assert.match(approvalFlow, /chatState\.activeApprovalId = ''/)
  assert.match(approvalFlow, /chatState\.loading = false/)
})

test('archiving current session selects the newest active session or requests a server UUID', () => {
  assert.deepEqual(chooseSessionAfterArchive([summary('recent')]), {
    sessionId: 'recent',
    create: false,
  })
  assert.deepEqual(chooseSessionAfterArchive([]), { sessionId: '', create: true })
})
