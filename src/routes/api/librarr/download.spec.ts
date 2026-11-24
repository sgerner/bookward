import { afterEach, describe, expect, it, vi } from "vitest";

const { engineMock } = vi.hoisted(() => ({ engineMock: vi.fn() }));

vi.mock("#lib/server/engine.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("#lib/server/engine.js")>()),
  engine: engineMock,
}));

import { POST } from "./download/+server";

afterEach(() => vi.clearAllMocks());

describe("librarr download endpoint", () => {
  it("forwards candidate_id and optional run_id to the engine", async () => {
    engineMock.mockResolvedValue({
      ok: true,
      media_type: "ebook",
      result: { id: "dl-1" },
      candidate: { id: 7, status: "imported" },
    });

    const response = await POST({
      request: new Request("http://bookward.test/api/librarr/download", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          media_type: "ebook",
          candidate_id: 7,
          run_id: "run-12345678",
          result: { title: "A Book", author: "A Writer" },
        }),
      }),
    } as never);

    expect(response.status).toBe(200);
    expect(engineMock).toHaveBeenCalledWith(
      "/api/librarr/download",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          media_type: "ebook",
          candidate_id: 7,
          run_id: "run-12345678",
          result: { title: "A Book", author: "A Writer" },
        }),
      }),
    );
    expect(await response.json()).toEqual({
      ok: true,
      media_type: "ebook",
      result: { id: "dl-1" },
      candidate: { id: 7, status: "imported" },
    });
  });

  it("rejects malformed run_id before contacting the engine", async () => {
    const response = await POST({
      request: new Request("http://bookward.test/api/librarr/download", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          media_type: "audiobook",
          candidate_id: 7,
          run_id: "short",
          result: { title: "A Book" },
        }),
      }),
    } as never);

    expect(response.status).toBe(400);
    expect(engineMock).not.toHaveBeenCalled();
  });
});
