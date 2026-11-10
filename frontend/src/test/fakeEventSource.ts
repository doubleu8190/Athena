import type {
  RunEventSource,
  RunEventSourceFactory,
} from "../features/chat/useRunStream";

type Listener = (event: MessageEvent<string>) => void;

export class FakeEventSource implements RunEventSource {
  static instances: FakeEventSource[] = [];

  readonly listeners = new Map<string, Listener[]>();
  readonly url: string;
  closed = false;
  closeCount = 0;
  onerror: ((event: Event) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;

  constructor(url: string | URL, _options?: EventSourceInit) {
    this.url = String(url);
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    const current = this.listeners.get(type) ?? [];
    this.listeners.set(type, [...current, listener]);
  }

  close(): void {
    this.closed = true;
    this.closeCount += 1;
  }

  emitOpen(): void {
    this.onopen?.(new Event("open"));
  }

  emitError(): void {
    this.onerror?.(new Event("error"));
  }

  emit(type: string, payload: unknown): void {
    const event = new MessageEvent(type, {
      data: JSON.stringify(payload),
    });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }

  emitRaw(type: string, data: string): void {
    const event = new MessageEvent(type, { data });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

export const fakeEventSourceFactory: RunEventSourceFactory = (url) =>
  new FakeEventSource(url);

