import type { ApprovalRequest, SessionSummary, ToolCallRecord } from '../api/client'

export type ApprovalUiState =
  | 'waiting'
  | 'running'
  | 'completed'
  | 'reapproval_required'
  | 'rejected'
  | 'expired'
  | 'uncertain'
  | 'cancelled'

export function resolveApprovalState(
  approval: ApprovalRequest,
  toolCalls: ToolCallRecord[],
  options: {
    activeApprovalId?: string
    loading?: boolean
    now?: number
  } = {},
): ApprovalUiState {
  if (options.activeApprovalId === approval.id && options.loading) return 'running'
  if (approval.status === 'rejected') return 'rejected'
  const latestRecord = [...toolCalls]
    .reverse()
    .find(call => call.arguments.approval_id === approval.id)
  if (approval.status === 'consumed') {
    return latestRecord?.status === 'ok' ? 'completed' : 'uncertain'
  }
  if ((options.now ?? Date.now()) >= new Date(approval.expires_at).getTime()) return 'expired'
  if ((approval.status === 'pending' || approval.status === 'approved') && latestRecord?.status === 'error') {
    return 'reapproval_required'
  }
  if (approval.last_resume_outcome === 'cancelled') return 'cancelled'
  return 'waiting'
}

export function isApprovalActionable(
  approval: ApprovalRequest,
  state: ApprovalUiState,
): boolean {
  if (approval.replacement_approval_id) return false
  if (!['pending', 'approved'].includes(approval.status)) return false
  return state === 'waiting' || state === 'cancelled' || state === 'reapproval_required'
}

export type ApprovalResumeOutcome = 'done' | 'error' | 'cancelled'

export function approvalResultNotice(
  state: ApprovalUiState | undefined,
  outcome: ApprovalResumeOutcome,
): string {
  if (state === 'completed') {
    if (outcome === 'done') return '工具已执行完成，续跑正常结束。'
    if (outcome === 'cancelled') return '工具已执行完成，但后续续跑已取消。'
    return '工具已执行完成，但后续续跑未完成。'
  }
  if (state === 'uncertain') return '结果不确定，不会自动重试。'
  if (outcome === 'done') return '续跑已结束，但获批工具未成功，请查看工具记录。'
  if (state === 'reapproval_required') {
    return '获批工具未成功，请查看工具记录后重试或重新审批。'
  }
  return outcome === 'cancelled'
    ? '续跑已取消，已同步会话与审批状态。'
    : '续跑未完成，已同步会话与审批状态。'
}

export type SessionOperation = 'rename' | 'archive' | 'unarchive'
export type ApprovalOperation = 'reject'

export async function runOperationOnce<T extends string>(
  active: Record<string, T | undefined>,
  errors: Record<string, string | undefined>,
  key: string,
  operation: T,
  action: () => Promise<void>,
  recoveryMessage: (message: string) => string,
): Promise<boolean> {
  if (active[key]) return false
  active[key] = operation
  delete errors[key]
  try {
    await action()
    return true
  } catch (error) {
    const message = error instanceof Error ? error.message : '操作失败。'
    errors[key] = recoveryMessage(message)
    throw error
  } finally {
    delete active[key]
  }
}

export function chooseSessionAfterArchive(
  activeSessions: SessionSummary[],
): { sessionId: string; create: boolean } {
  if (activeSessions[0]) return { sessionId: activeSessions[0].session_id, create: false }
  return { sessionId: '', create: true }
}
