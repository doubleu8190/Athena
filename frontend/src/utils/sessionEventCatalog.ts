import { ApplicationEventType } from "../types/events.ts"

/** EventSource 监听的完整聊天事件集合；不会订阅 Trace 事件。 */
export const SESSION_EVENT_NAMES = Object.values(ApplicationEventType)
