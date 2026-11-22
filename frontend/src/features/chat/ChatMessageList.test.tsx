import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { makeMessage } from "../../test/fixtures";
import {
  ChatMessageList,
  type ChatAutoScrollRequest,
} from "./ChatMessageList";

const SESSION_ID = "session-scroll";

function makeMessageList(
  overrides: Partial<Parameters<typeof ChatMessageList>[0]> = {},
) {
  return (
    <ChatMessageList
      autoScrollRequest={null}
      error={null}
      hasMore={false}
      isLoading={false}
      isLoadingMore={false}
      messages={[
        makeMessage({
          id: "message-1",
          parts: [{ text: "较早消息", type: "text" }],
          role: "user",
          sequence: 1,
          session_id: SESSION_ID,
        }),
        makeMessage({
          id: "message-2",
          parts: [{ text: "最新回答", type: "text" }],
          sequence: 2,
          session_id: SESSION_ID,
        }),
      ]}
      onLoadMore={() => undefined}
      onRetry={() => undefined}
      sessionId={SESSION_ID}
      transientText=""
      transientUnfinished={false}
      {...overrides}
    />
  );
}

function setMessageListMetrics(node: HTMLElement, initialHeight: number) {
  let scrollHeight = initialHeight;
  Object.defineProperties(node, {
    clientHeight: {
      configurable: true,
      value: 300,
    },
    scrollHeight: {
      configurable: true,
      get: () => scrollHeight,
    },
    scrollTop: {
      configurable: true,
      value: 0,
      writable: true,
    },
  });
  return {
    setScrollHeight(nextHeight: number) {
      scrollHeight = nextHeight;
    },
  };
}

function stubScrollIntoView() {
  const scrollIntoView = vi.fn();
  Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
    configurable: true,
    value: scrollIntoView,
    writable: true,
  });
  return scrollIntoView;
}

describe("会话消息滚动", () => {
  it("首次载入定位最新，流式增长只跟随消息容器且不夺焦点", () => {
    const scrollHeightSpy = vi
      .spyOn(HTMLElement.prototype, "scrollHeight", "get")
      .mockReturnValue(900);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
    const windowScrollSpy = vi
      .spyOn(window, "scrollTo")
      .mockImplementation(() => undefined);
    const scrollIntoView = stubScrollIntoView();
    const { rerender } = render(makeMessageList());
    const list = screen.getByRole("region", { name: "消息历史" });

    expect(list.scrollTop).toBe(900);
    list.focus();
    scrollHeightSpy.mockReturnValue(1200);
    rerender(makeMessageList({ transientText: "第一段实时输出" }));

    expect(list.scrollTop).toBe(1200);
    expect(list).toHaveFocus();
    expect(windowScrollSpy).not.toHaveBeenCalled();
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it("主动上滑后停止跟随，并可通过回到最新恢复", () => {
    const scrollHeightSpy = vi
      .spyOn(HTMLElement.prototype, "scrollHeight", "get")
      .mockReturnValue(900);
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
    const scrollIntoView = stubScrollIntoView();
    const { rerender } = render(makeMessageList());
    const list = screen.getByRole("region", { name: "消息历史" });

    list.scrollTop = 100;
    fireEvent.scroll(list);
    expect(
      screen.getByRole("button", { name: "回到最新" }),
    ).toBeInTheDocument();

    scrollHeightSpy.mockReturnValue(1200);
    rerender(
      makeMessageList({
        transientText: "用户离开底部后继续增长的实时输出",
      }),
    );
    expect(list.scrollTop).toBe(100);

    fireEvent.click(screen.getByRole("button", { name: "回到最新" }));
    expect(list.scrollTop).toBe(1200);
    expect(scrollIntoView).toHaveBeenCalledWith({
      block: "nearest",
      inline: "nearest",
    });
    expect(
      screen.queryByRole("button", { name: "回到最新" }),
    ).not.toBeInTheDocument();
  });

  it("加载更早消息退出跟随，不把新增历史当成最新消息", () => {
    const onLoadMore = vi.fn();
    const { rerender } = render(
      makeMessageList({
        hasMore: true,
        onLoadMore,
      }),
    );
    const list = screen.getByRole("region", { name: "消息历史" });
    const metrics = setMessageListMetrics(list, 1000);

    list.scrollTop = 100;
    fireEvent.scroll(list);
    fireEvent.click(
      screen.getByRole("button", { name: "加载更早消息" }),
    );
    expect(onLoadMore).toHaveBeenCalledOnce();

    metrics.setScrollHeight(1300);
    rerender(
      makeMessageList({
        hasMore: false,
        isLoadingMore: true,
        messages: [
          makeMessage({
            id: "message-0",
            parts: [{ text: "刚加载的更早消息", type: "text" }],
            role: "user",
            sequence: 0,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-1",
            parts: [{ text: "较早消息", type: "text" }],
            role: "user",
            sequence: 1,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-2",
            parts: [{ text: "最新回答", type: "text" }],
            sequence: 2,
            session_id: SESSION_ID,
          }),
        ],
        onLoadMore,
      }),
    );

    expect(list.scrollTop).toBe(100);
    expect(
      screen.getByRole("button", { name: "回到最新" }),
    ).toBeInTheDocument();
  });

  it("补充回答只在对应新用户消息进入列表后回到最新", () => {
    const request: ChatAutoScrollRequest = {
      afterSequence: 2,
      key: "answer-request",
      text: "补充回答内容",
    };
    const { rerender } = render(makeMessageList());
    const list = screen.getByRole("region", { name: "消息历史" });
    const metrics = setMessageListMetrics(list, 1000);
    const scrollIntoView = stubScrollIntoView();

    list.scrollTop = 100;
    fireEvent.scroll(list);
    rerender(makeMessageList({ autoScrollRequest: request }));
    expect(list.scrollTop).toBe(100);

    metrics.setScrollHeight(1200);
    rerender(
      makeMessageList({
        autoScrollRequest: request,
        messages: [
          makeMessage({
            id: "message-1",
            parts: [{ text: "较早消息", type: "text" }],
            role: "user",
            sequence: 1,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-2",
            parts: [{ text: "最新回答", type: "text" }],
            sequence: 2,
            session_id: SESSION_ID,
          }),
          makeMessage({
            id: "message-3",
            parts: [{ text: "补充回答内容", type: "text" }],
            role: "user",
            sequence: 3,
            session_id: SESSION_ID,
          }),
        ],
      }),
    );

    expect(list.scrollTop).toBe(1200);
    expect(scrollIntoView).toHaveBeenCalledWith({
      block: "nearest",
      inline: "nearest",
    });
    expect(
      screen.queryByRole("button", { name: "回到最新" }),
    ).not.toBeInTheDocument();
  });
});
