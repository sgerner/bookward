import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "./+server";
import { POST as testPOST } from "../test/+server";

afterEach(() => vi.unstubAllGlobals());

describe("/api/llm routes", () => {
  it("forwards local Ollama discovery query parameters and preserves HTTP 400 SSRF rejections", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            mode: "ollama",
            api_url: "http://ollama:11434",
            models: [{ id: "qwen2.5:7b", name: "qwen2.5:7b" }],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: "Local addresses are not allowed" }),
          { status: 400, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const okRes = await GET({
      url: new URL(
        "http://afterword.test/api/llm/models?mode=ollama&api_url=http%3A%2F%2Follama%3A11434&use_saved_key=true",
      ),
    } as never);
    expect(okRes.status).toBe(200);
    await expect(okRes.json()).resolves.toMatchObject({
      mode: "ollama",
      models: [{ id: "qwen2.5:7b", name: "qwen2.5:7b" }],
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/llm/models?mode=ollama&api_url=http%3A%2F%2Follama%3A11434&use_saved_key=true",
    );

    const blockedRes = await GET({
      url: new URL(
        "http://afterword.test/api/llm/models?mode=ollama&api_url=http%3A%2F%2Flocalhost%3A11434",
      ),
    } as never);
    expect(blockedRes.status).toBe(400);
    await expect(blockedRes.json()).resolves.toEqual({
      message: "Local addresses are not allowed",
    });
  });

  it("sends unsaved API keys in POST body for model discovery and connection testing", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            mode: "openai-compatible",
            models: [{ id: "Qwen/Qwen2.5-7B-Instruct", name: "Qwen/Qwen2.5-7B-Instruct" }],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            mode: "ollama",
            model: "qwen2.5:7b",
            latency_ms: 14,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    const discRes = await POST({
      request: new Request("http://afterword.test/api/llm/models", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          mode: "openai-compatible",
          api_url: "http://vllm:8000/v1",
          api_key: "secret-key",
          use_saved_key: true,
        }),
      }),
    } as never);
    expect(discRes.status).toBe(200);
    expect(
      JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body)),
    ).toEqual({
      mode: "openai-compatible",
      api_url: "http://vllm:8000/v1",
      api_key: "secret-key",
      use_saved_key: true,
    });

    const probeRes = await testPOST({
      request: new Request("http://afterword.test/api/llm/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          mode: "ollama",
          api_url: "http://ollama:11434",
          model: "qwen2.5:7b",
        }),
      }),
    } as never);
    expect(probeRes.status).toBe(200);
    await expect(probeRes.json()).resolves.toMatchObject({
      ok: true,
      mode: "ollama",
      model: "qwen2.5:7b",
    });
  });
});
