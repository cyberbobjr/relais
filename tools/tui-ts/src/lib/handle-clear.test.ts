import { test, expect, mock, beforeEach } from "bun:test";
import { handleClear, type ClearDeps } from "./handle-clear.ts";

const makeDoneEvent = (sessionId = "sess-abc") => ({
  type: "done" as const,
  content: "✓ Conversation history cleared.",
  correlationId: "corr-123",
  sessionId,
});

function makeDeps(overrides: Partial<ClearDeps> = {}): ClearDeps {
  return {
    client: { sendMessage: mock(async () => makeDoneEvent()) },
    sessionId: "sess-abc",
    clearMessages: mock(() => {}),
    onSessionId: mock(() => {}),
    setErrorBanner: mock(() => {}),
    setCopyFlash: mock(() => {}),
    ...overrides,
  };
}

// Test 1: UI is cleared immediately (before backend responds)
test("clears messages immediately on /clear", async () => {
  let clearedBeforeBackend = false;
  let backendCalled = false;

  const deps = makeDeps({
    clearMessages: mock(() => {
      clearedBeforeBackend = !backendCalled;
    }),
    client: {
      sendMessage: mock(async () => {
        backendCalled = true;
        return makeDoneEvent();
      }),
    },
  });

  await handleClear(deps);

  expect(deps.clearMessages).toHaveBeenCalledTimes(1);
  expect(clearedBeforeBackend).toBe(true);
});

// Test 2: backend is called with /clear and the current sessionId
test("sends /clear to the backend with the current sessionId", async () => {
  const deps = makeDeps({ sessionId: "session-xyz" });

  await handleClear(deps);

  expect(deps.client.sendMessage).toHaveBeenCalledTimes(1);
  expect(deps.client.sendMessage).toHaveBeenCalledWith("/clear", { sessionId: "session-xyz" });
});

// Test 2b: works with empty sessionId (new session, no history yet)
test("sends /clear without sessionId when sessionId is empty", async () => {
  const deps = makeDeps({ sessionId: "" });

  await handleClear(deps);

  expect(deps.client.sendMessage).toHaveBeenCalledWith("/clear", { sessionId: undefined });
});

// Test 3: on success, drops the persisted sessionId
test("calls onSessionId('') on success to drop persisted session", async () => {
  const deps = makeDeps();

  await handleClear(deps);

  expect(deps.onSessionId).toHaveBeenCalledTimes(1);
  expect(deps.onSessionId).toHaveBeenCalledWith("");
});

// Test 4: on success, shows a flash confirmation
test("shows a flash message on success", async () => {
  const deps = makeDeps();

  await handleClear(deps);

  expect(deps.setCopyFlash).toHaveBeenCalledTimes(1);
  const flashCall = (deps.setCopyFlash as ReturnType<typeof mock>).mock.calls[0];
  expect(flashCall).toBeDefined();
  expect((flashCall![0] as string).length).toBeGreaterThan(0);
});

// Test 5: on backend error, sets the error banner
test("sets errorBanner on backend failure", async () => {
  const deps = makeDeps({
    client: {
      sendMessage: mock(async () => {
        throw new Error("HTTP 403: Forbidden");
      }),
    },
  });

  await handleClear(deps);

  expect(deps.setErrorBanner).toHaveBeenCalledTimes(1);
  const bannerCall = (deps.setErrorBanner as ReturnType<typeof mock>).mock.calls[0];
  expect(bannerCall).toBeDefined();
  expect(bannerCall![0] as string).toContain("Clear failed");
});

// Test 6: on backend error, does NOT call onSessionId
test("does not drop sessionId on backend failure", async () => {
  const deps = makeDeps({
    client: {
      sendMessage: mock(async () => {
        throw new Error("Network error");
      }),
    },
  });

  await handleClear(deps);

  expect(deps.onSessionId).not.toHaveBeenCalled();
});

// Test 7: non-clear commands are not affected (guard — handleClear is only called for /clear)
// This is a documentation test: handleClear does not check the command,
// it is the caller's responsibility to only invoke it for /clear.
// We verify it does call the backend (i.e., it is not a no-op).
test("handleClear always calls the backend (caller gates on /clear)", async () => {
  const deps = makeDeps();
  await handleClear(deps);
  expect(deps.client.sendMessage).toHaveBeenCalled();
});
