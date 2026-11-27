import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "./+server";
import { POST as testPOST } from "../test/+server";

afterEach(() => vi.unstubAllGlobals());

describe("/api/llm routes", () => {
  it("strips use_saved_key from GET model discovery and preserves HTTP 400 SSRF rejections", async () => {
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
      "http://127.0.0.1:8000/api/llm/models?mode=ollama&api_url=http%3A%2F%2Follama%3A11434",
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

  it("defaults use_saved_key to false on POST /api/llm/models and POST /api/llm/test unless explicitly true", async () => {
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
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            mode: "cloud",
            model: "gpt-4o-mini",
            latency_ms: 21,
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
      use_saved_key: false,
    });

    const defaultTestRes = await testPOST({
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
    expect(defaultTestRes.status).toBe(200);
    expect(
      JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body)),
    ).toEqual({
      mode: "ollama",
      api_url: "http://ollama:11434",
      model: "qwen2.5:7b",
      api_key: "",
      use_saved_key: false,
    });

    const explicitSavedKeyRes = await testPOST({
      request: new Request("http://afterword.test/api/llm/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          mode: "cloud",
          api_url: "https://api.openai.com/v1/chat/completions",
          model: "gpt-4o-mini",
          use_saved_key: true,
        }),
      }),
    } as never);
    expect(explicitSavedKeyRes.status).toBe(200);
    expect(
      JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body)),
    ).toEqual({
      mode: "cloud",
      api_url: "https://api.openai.com/v1/chat/completions",
      model: "gpt-4o-mini",
      api_key: "",
      use_saved_key: true,
    });
  });

  it("rejects unknown mode values with HTTP 400 without contacting the engine", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const badGet = await GET({
      url: new URL("http://afterword.test/api/llm/models?mode=bogus"),
    } as never);
    expect(badGet.status).toBe(400);
    await expect(badGet.json()).resolves.toEqual({
      message: "Choose a valid LLM reranker mode.",
    });

    const badPostModels = await POST({
      request: new Request("http://afterword.test/api/llm/models", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ mode: "bogus", api_url: "http://ollama:11434" }),
      }),
    } as never);
    expect(badPostModels.status).toBe(400);
    await expect(badPostModels.json()).resolves.toEqual({
      message: "Choose a valid LLM reranker mode.",
    });

    const badPostTest = await testPOST({
      request: new Request("http://afterword.test/api/llm/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          mode: "bogus",
          api_url: "http://ollama:11434",
          model: "qwen2.5:7b",
        }),
      }),
    } as never);
    expect(badPostTest.status).toBe(400);
    await expect(badPostTest.json()).resolves.toEqual({
      message: "Choose a valid LLM reranker mode.",
    });

    expect(fetchMock).not.toHaveBeenCalled();
  });
});
