import type { MessageItem } from '../api/client'

export function recoverStreamMessages(
  persistedMessages: MessageItem[] | undefined,
  fallbackMessages: MessageItem[],
): MessageItem[] {
  if (!persistedMessages) {
    return [...fallbackMessages]
  }
  return persistedMessages.filter(
    message => message.role !== 'assistant' || (typeof message.content === 'string' && message.content.length > 0),
  )
}
