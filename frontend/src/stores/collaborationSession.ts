import type { CollaborationInfo, CollaborationSummary } from '../api/client'

export function selectRoomForSession(
  currentRoom: CollaborationInfo | null,
  rooms: CollaborationSummary[],
  sessionId: string,
): string | null {
  if (
    currentRoom?.session_id === sessionId
    && rooms.some(room => room.id === currentRoom.id && room.session_id === sessionId)
  ) {
    return currentRoom.id
  }
  return rooms.find(room => room.session_id === sessionId)?.id ?? null
}
