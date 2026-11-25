/**
 * 基于真实 ReadableStream + text/event-stream Response 的 SSE 测试辅助流：
 * 配合 Vitest fetch stub，使组件与 Hook 测试完整覆盖原生 fetch + eventsource-parser 解析链路。
 */
export class FakeSseStream {
  static instances: FakeSseStream[] = [];

  readonly url: string;
  readonly init: RequestInit | undefined;
  readonly response: Response;
  closed = false;
  closeCount = 0;

  private controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  private readonly encoder = new TextEncoder();

  constructor(
    url: string,
    init?: RequestInit,
    status = 200,
    headers?: HeadersInit,
  ) {
    this.url = url;
    this.init = init;
    const responseHeaders = new Headers({
      "Content-Type": "text/event-stream; charset=utf-8",
    });
    if (headers) {
      new Headers(headers).forEach((value, key) => {
        responseHeaders.set(key, value);
      });
    }
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
      cancel: () => {
        this.markClosed();
      },
    });
    if (init?.signal) {
      if (init.signal.aborted) {
        this.markClosed();
      } else {
        init.signal.addEventListener(
          "abort",
          () => {
            this.markClosed();
            try {
              this.controller?.close();
            } catch {
              // 流已被上游关闭时忽略重复关闭。
            }
          },
          { once: true },
        );
      }
    }
    this.response = new Response(stream, {
      headers: responseHeaders,
      status,
    });
    FakeSseStream.instances.push(this);
  }

  private markClosed(): void {
    if (!this.closed) {
      this.closed = true;
      this.closeCount += 1;
    }
  }

  /** 发送标准 SSE 事件（event + 可选 id + JSON data + 空行）。 */
  emit(event: string, payload: unknown, id?: string): void {
    const lines: string[] = [`event: ${event}`];
    if (id !== undefined) {
      lines.push(`id: ${id}`);
    }
    lines.push(`data: ${JSON.stringify(payload)}`);
    this.enqueueText(`${lines.join("\n")}\n\n`);
  }

  /** 发送原始 data 字符串（用于验证非法 JSON 等容错分支）。 */
  emitRaw(event: string, data: string): void {
    this.enqueueText(`event: ${event}\ndata: ${data}\n\n`);
  }

  /** 写入原始 SSE 文本（用于测试 retry: / 多行 data: / 注释行等协议特性）。 */
  enqueueText(text: string): void {
    this.enqueueBytes(this.encoder.encode(text));
  }

  /** 写入任意字节分片（用于测试跨分片 UTF-8 多字节字符重组）。 */
  enqueueBytes(bytes: Uint8Array): void {
    if (this.closed || !this.controller) {
      return;
    }
    this.controller.enqueue(bytes);
  }

  /** 模拟服务端结束流（EOF）。 */
  end(): void {
    if (this.closed || !this.controller) {
      return;
    }
    this.markClosed();
    try {
      this.controller.close();
    } catch {
      // 忽略已关闭状态。
    }
  }

  /** 模拟传输层异常断流，使 reader.read() 抛出异常。 */
  error(err: unknown = new TypeError("SSE stream disconnected")): void {
    if (this.closed || !this.controller) {
      return;
    }
    this.markClosed();
    try {
      this.controller.error(err);
    } catch {
      // 忽略已关闭状态。
    }
  }
}

export function createSseStreamResponse(
  url: string,
  init?: RequestInit,
  status = 200,
  headers?: HeadersInit,
): Response {
  return new FakeSseStream(url, init, status, headers).response;
}
