<script lang="ts">
  import { enhance } from "$app/forms";
  import { onDestroy } from "svelte";
  import type { SubmitFunction } from "@sveltejs/kit";
  import { Check, ExternalLink, KeyRound, Link2, LogOut, Play, RefreshCw, ShieldCheck, X } from "@lucide/svelte";

  type ProviderModel = { id: string; name: string; family: string; reasoning: boolean; structured_output: boolean; tool_call: boolean; context: number; output: number; input_cost: number | null; output_cost: number | null };
  type Provider = { id: string; name: string; api: string; models: ProviderModel[] };
  type Catalog = { providers?: Provider[]; fetched_at?: string; stale?: boolean };
  type SubscriptionModel = { id: string; displayName?: string; defaultReasoningEffort?: string; isDefault?: boolean; supportedReasoningEfforts?: Array<{ reasoningEffort: string; description?: string }> };
  type DeviceLogin = { status?: string; authenticated?: boolean; login_id?: string; verification_url?: string; user_code?: string; account?: { email?: string | null; planType?: string | null; plan_type?: string | null }; models?: SubscriptionModel[]; connection?: { id?: number; name?: string; model_id?: string }; policy?: { id?: number; name?: string; reasoning_effort?: string }; error?: string };
  type Connection = { id: number; name: string; provider_id: string; model_id: string; endpoint: string; auth_type: string; enabled: number; last_status: string | null; last_used_at: string | null };
  type Policy = { id: number; name: string; connection_id: number; enabled: number; top_k: number; prompt_version: string; connection_name: string; provider_id: string; model_id: string; auth_type: string; reasoning_effort: string };
  type Run = { id: string; policy_id: number; connection_id: number; status: string; candidate_count: number; latency_ms: number | null; created_at: string; finished_at: string | null };
  type LlmData = { connections: Connection[]; policies: Policy[]; runs: Run[] };
  type FormState = { llmCatalog?: Catalog; deviceLogin?: DeviceLogin; llmRuns?: Run[] } | null | undefined;
  type ConnectionMethod = "api" | "chatgpt" | "claude";
  type OptimisticChange = { commit?: () => void; rollback?: () => void };
  type OptimisticFactory = (formData: FormData) => OptimisticChange | void;
  const reasoningLevels = ["low", "medium", "high", "xhigh", "max"] as const;

  let { llm, formState, setPending, isPending }: { llm: LlmData; formState: FormState; setPending: (key: string, optimistic?: OptimisticFactory) => SubmitFunction; isPending: (key: string) => boolean } = $props();
  let catalog = $state<Catalog | null>(null);
  let deviceLogin = $state<DeviceLogin | null>(null);
  let runs = $state<Run[]>([]);
  let catalogQuery = $state("");
  let providerSearchOpen = $state(false);
  let selectedProviderId = $state("");
  let selectedModelId = $state("");
  let modelChoice = $state("");
  let chatgptModelId = $state("");
  let chatgptReasoningEffort = $state("medium");
  let chatgptServerModelId = $state("");
  let chatgptServerReasoningEffort = $state("");
  let connectionEnabledOverrides = $state<Record<number, number>>({});
  let policyOverrides = $state<Record<number, Partial<Policy>>>({});
  let connectionMethod = $state<ConnectionMethod>("api");
  let deviceStatusForm = $state<HTMLFormElement>();
  let deviceStatusRequested = $state(false);
  let catalogLoadForm = $state<HTMLFormElement>();
  let catalogRequested = $state(false);

  $effect(() => { runs = llm.runs; });
  $effect(() => {
    if (formState?.llmCatalog) catalog = formState.llmCatalog;
    if (formState?.deviceLogin) deviceLogin = formState.deviceLogin;
    if (formState?.llmRuns) runs = formState.llmRuns;
  });
  $effect(() => {
    const modelId = deviceLogin?.connection?.model_id ?? "";
    if (modelId && modelId !== chatgptServerModelId) {
      chatgptModelId = modelId;
      chatgptServerModelId = modelId;
    }
    const reasoningEffort = deviceLogin?.policy?.reasoning_effort ?? "";
    if (reasoningEffort && reasoningEffort !== chatgptServerReasoningEffort) {
      chatgptReasoningEffort = reasoningEffort;
      chatgptServerReasoningEffort = reasoningEffort;
    }
  });
  $effect(() => {
    if (!catalog && !catalogRequested && catalogLoadForm) {
      catalogRequested = true;
      catalogLoadForm.requestSubmit();
    }
  });
  $effect(() => {
    if (!selectedProviderId && catalog?.providers?.length) {
      selectedProviderId = catalog.providers[0].id;
      selectedModelId = catalog.providers[0].models[0]?.id ?? "";
      modelChoice = `${selectedProviderId}::${selectedModelId}`;
    }
  });
  $effect(() => {
    if (!deviceStatusRequested && deviceStatusForm) {
      deviceStatusRequested = true;
      deviceStatusForm.requestSubmit();
    }
  });
  $effect(() => {
    if (deviceLogin?.status !== "pending") return;
    const timer = window.setInterval(() => {
      if (deviceStatusForm && !isPending("openai-status")) deviceStatusForm.requestSubmit();
    }, 2500);
    return () => window.clearInterval(timer);
  });
  onDestroy(() => { deviceStatusForm = undefined; catalogLoadForm = undefined; });

  const filteredProviders = $derived((catalog?.providers ?? []).map((provider) => ({
    ...provider,
    models: provider.models.filter((model) => {
      const query = catalogQuery.trim().toLowerCase();
      return !query || `${provider.name} ${provider.id} ${model.name} ${model.id}`.toLowerCase().includes(query);
    }),
  })).filter((provider) => provider.models.length));
  const matchingModels = $derived(filteredProviders.flatMap((provider) => provider.models.map((model) => ({ provider, model }))));
  const selectedProvider = $derived(catalog?.providers?.find((provider) => provider.id === selectedProviderId) ?? null);
  const selectedChatgptModel = $derived((deviceLogin?.models ?? []).find((model) => model.id === chatgptModelId));
  const chatgptReasoningLevels = $derived(selectedChatgptModel?.supportedReasoningEfforts?.map((item) => item.reasoningEffort.toLowerCase()).filter((level) => reasoningLevels.includes(level as (typeof reasoningLevels)[number])) ?? [...reasoningLevels]);
  const visibleConnections = $derived(llm.connections.map((connection) => ({
    ...connection,
    enabled: connectionEnabledOverrides[connection.id] ?? connection.enabled,
  })));
  const visiblePolicies = $derived(llm.policies.map((policy) => ({
    ...policy,
    ...(policyOverrides[policy.id] ?? {}),
  })));
  const claudePolicy = $derived(visiblePolicies.find((policy) => policy.auth_type === "claude_code") ?? null);
  const anthropicModels = $derived(catalog?.providers?.find((provider) => provider.id === "anthropic")?.models ?? []);

  function selectModel(value: string) {
    modelChoice = value;
    const separator = value.indexOf("::");
    if (separator > 0) {
      selectedProviderId = value.slice(0, separator);
      selectedModelId = value.slice(separator + 2);
    }
  }
  function selectCatalogModel(provider: Provider, model: ProviderModel) {
    selectedProviderId = provider.id;
    selectedModelId = model.id;
    modelChoice = `${selectedProviderId}::${selectedModelId}`;
    catalogQuery = provider.name;
    providerSearchOpen = false;
  }
  function selectChatgptModel(value: string) {
    chatgptModelId = value;
    const model = (deviceLogin?.models ?? []).find((item) => item.id === value);
    const supported = model?.supportedReasoningEfforts?.map((item) => item.reasoningEffort.toLowerCase()).filter((level) => reasoningLevels.includes(level as (typeof reasoningLevels)[number])) ?? [];
    if (supported.length && !supported.includes(chatgptReasoningEffort)) chatgptReasoningEffort = model?.defaultReasoningEffort?.toLowerCase() ?? supported[0];
  }
  function formatDate(value: string | null | undefined) {
    if (!value) return "—";
    const parsed = new Date(value.replace(" ", "T"));
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }
  function authLabel(value: string) {
    return value === "openai_codex" ? "ChatGPT subscription" : value === "claude_code" ? "Claude Code OAuth" : "API key";
  }

  function optimisticDisableConnection(formData: FormData) {
    const id = Number(formData.get("id"));
    const connection = llm.connections.find((item) => item.id === id);
    if (!connection) return;
    const previous = connectionEnabledOverrides[id];
    connectionEnabledOverrides = { ...connectionEnabledOverrides, [id]: 0 };
    return {
      commit: () => {
        const next = { ...connectionEnabledOverrides };
        delete next[id];
        connectionEnabledOverrides = next;
      },
      rollback: () => {
        const next = { ...connectionEnabledOverrides };
        if (previous === undefined) delete next[id];
        else next[id] = previous;
        connectionEnabledOverrides = next;
      },
    };
  }

  function optimisticPolicySettings(formData: FormData) {
    const id = Number(formData.get("policyId"));
    const modelId = String(formData.get("modelId") ?? "");
    const reasoningEffort = String(formData.get("reasoningEffort") ?? "");
    if (!Number.isInteger(id) || !modelId || !reasoningEffort) return;
    const previous = policyOverrides[id];
    policyOverrides = {
      ...policyOverrides,
      [id]: { ...(previous ?? {}), model_id: modelId, reasoning_effort: reasoningEffort },
    };
    return {
      commit: () => {
        const next = { ...policyOverrides };
        delete next[id];
        policyOverrides = next;
      },
      rollback: () => {
        const next = { ...policyOverrides };
        if (previous === undefined) delete next[id];
        else next[id] = previous;
        policyOverrides = next;
      },
    };
  }

  function optimisticDisablePolicy(formData: FormData) {
    const id = Number(formData.get("id"));
    const policy = llm.policies.find((item) => item.id === id);
    if (!policy) return;
    const previous = policyOverrides[id];
    policyOverrides = { ...policyOverrides, [id]: { ...(previous ?? {}), enabled: 0 } };
    return {
      commit: () => {
        const next = { ...policyOverrides };
        delete next[id];
        policyOverrides = next;
      },
      rollback: () => {
        const next = { ...policyOverrides };
        if (previous === undefined) delete next[id];
        else next[id] = previous;
        policyOverrides = next;
      },
    };
  }
</script>

<section class="mt-6 space-y-5 lg:col-span-2" aria-label="LLM settings">
  <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="llm-connections-heading">
    <div class="mb-5 flex flex-wrap items-start justify-between gap-4">
      <div class="flex min-w-0 items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-primary"><Link2 size={19} /></span><div><h3 id="llm-connections-heading" class="text-lg font-semibold text-surface-950-50">Connect a LLM</h3></div></div>
      <form method="POST" action="?/loadLlmCatalog" use:enhance={setPending("llm-catalog")}><input type="hidden" name="refresh" value="on" /><button class="btn btn-sm min-h-10 preset-tonal-primary" type="submit" disabled={isPending("llm-catalog")} aria-busy={isPending("llm-catalog")}>{#if isPending("llm-catalog")}<RefreshCw size={15} class="animate-spin" />{:else}<RefreshCw size={15} />{/if} {catalog ? "Refresh catalog" : "Load catalog"}</button></form>
    </div>

    <div class="grid gap-2 sm:grid-cols-3" role="tablist" aria-label="Connection method">
      <button type="button" role="tab" aria-selected={connectionMethod === "api"} class={`min-h-11 border p-3 text-left ${connectionMethod === "api" ? "border-primary-500 preset-tonal-primary" : "border-surface-300-700/50 preset-tonal-surface"}`} onclick={() => connectionMethod = "api"}><strong class="block text-sm">API key</strong><span class="mt-1 block text-xs text-surface-700-300">OpenAI-compatible providers</span></button>
      <button type="button" role="tab" aria-selected={connectionMethod === "chatgpt"} class={`min-h-11 border p-3 text-left ${connectionMethod === "chatgpt" ? "border-primary-500 preset-tonal-primary" : "border-surface-300-700/50 preset-tonal-surface"}`} onclick={() => connectionMethod = "chatgpt"}><strong class="block text-sm">ChatGPT</strong><span class="mt-1 block text-xs text-surface-700-300">Subscription device login</span></button>
      <button type="button" role="tab" aria-selected={connectionMethod === "claude"} class={`min-h-11 border p-3 text-left ${connectionMethod === "claude" ? "border-primary-500 preset-tonal-primary" : "border-surface-300-700/50 preset-tonal-surface"}`} onclick={() => connectionMethod = "claude"}><strong class="block text-sm">Claude</strong><span class="mt-1 block text-xs text-surface-700-300">Claude Code OAuth</span></button>
    </div>
    <form class="hidden" method="POST" action="?/loadLlmCatalog" bind:this={catalogLoadForm} use:enhance={setPending("llm-catalog-auto")}><button type="submit">Load catalog</button></form>
    <form class="hidden" method="POST" action="?/openaiDeviceLoginStatus" bind:this={deviceStatusForm} use:enhance={setPending("openai-status")}><button type="submit">Check status</button></form>

    {#if connectionMethod === "chatgpt"}
      <div class="mt-5 border border-surface-300-700/50 p-4 text-sm" role="status" aria-live="polite">
        {#if deviceLogin?.status === "pending"}<p class="font-semibold text-surface-950-50">Waiting for authorization</p><p class="mt-1 text-surface-700-300">Code: <strong class="text-surface-950-50">{deviceLogin.user_code || "—"}</strong></p>{#if deviceLogin.verification_url}<a class="mt-3 inline-flex items-center gap-2 text-primary-600-400 underline" href={deviceLogin.verification_url} target="_blank" rel="noreferrer">Open verification page <ExternalLink size={14} /></a>{/if}<p class="mt-3 text-xs text-surface-700-300">Bookward checks automatically every few seconds.</p>{:else if deviceLogin?.authenticated}<p class="font-semibold text-success-600-400">Signed in{deviceLogin.account?.email ? ` as ${deviceLogin.account.email}` : ""}.</p>{#if deviceLogin.connection?.model_id}<p class="mt-1 text-xs text-surface-700-300">Connection ready with {deviceLogin.connection.model_id}; its shadow policy was created automatically.</p>{/if}{:else if deviceLogin?.status === "failed"}<p class="font-semibold text-error-600-400">Device login failed.</p><p class="mt-1 text-xs text-error-600-400">{deviceLogin.error ?? "Try starting the device login again."}</p>{:else if deviceLogin?.status === "cancelled"}<p class="text-surface-700-300">Device login cancelled.</p>{:else}<p class="text-surface-700-300">No ChatGPT subscription is connected.</p>{/if}
      </div>
      {#if deviceLogin?.authenticated && deviceLogin.policy?.id && (deviceLogin.models ?? []).length}
        <form class="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_12rem_auto] sm:items-end" method="POST" action="?/saveLlmPolicySettings" use:enhance={setPending("llm-chatgpt-settings", optimisticPolicySettings)}>
          <input type="hidden" name="policyId" value={deviceLogin.policy.id} />
          <label class="block text-sm font-medium text-surface-800-200">Shadow model<select class="input mt-2" name="modelId" value={chatgptModelId} oninput={(event) => selectChatgptModel(event.currentTarget.value)}>{#each (deviceLogin.models ?? []) as model (model.id)}<option value={model.id}>{model.displayName ?? model.id}</option>{/each}</select></label>
          <label class="block text-sm font-medium text-surface-800-200">Reasoning<select class="input mt-2" name="reasoningEffort" value={chatgptReasoningEffort} oninput={(event) => chatgptReasoningEffort = event.currentTarget.value}>{#each chatgptReasoningLevels as effort}<option value={effort}>{effort}</option>{/each}</select></label>
          <button class="btn min-h-10 preset-tonal-primary" type="submit" disabled={isPending("llm-chatgpt-settings")} aria-busy={isPending("llm-chatgpt-settings")}>{isPending("llm-chatgpt-settings") ? "Saving…" : "Save settings"}</button>
        </form>
      {/if}
      <div class="mt-4 flex flex-wrap gap-2">{#if deviceLogin?.status === "pending"}<form method="POST" action="?/openaiDeviceLoginCancel" use:enhance={setPending("openai-cancel")}><input type="hidden" name="loginId" value={deviceLogin.login_id ?? ""} /><button class="btn btn-sm min-h-10 preset-tonal-error" type="submit" disabled={isPending("openai-cancel")}><X size={15} /> Cancel</button></form>{:else}<form method="POST" action="?/openaiDeviceLoginStart" use:enhance={setPending("openai-start")}><button class="btn btn-sm min-h-10 preset-filled-primary-500" type="submit" disabled={isPending("openai-start")} aria-busy={isPending("openai-start")}>{#if isPending("openai-start")}<RefreshCw size={15} class="animate-spin" />{:else}<KeyRound size={15} />{/if} Start device login</button></form>{/if}<form method="POST" action="?/openaiDeviceLogout" use:enhance={setPending("openai-logout")}><button class="btn btn-sm min-h-10 preset-tonal-surface" type="submit" disabled={isPending("openai-logout")}><LogOut size={15} /> Sign out</button></form></div>
    {:else}
      {#if connectionMethod === "claude" && claudePolicy}
        <form class="mt-5 grid gap-3 sm:grid-cols-[minmax(0,1fr)_12rem_auto] sm:items-end" method="POST" action="?/saveLlmPolicySettings" use:enhance={setPending("llm-claude-settings", optimisticPolicySettings)}>
          <input type="hidden" name="policyId" value={claudePolicy.id} />
          <label class="block text-sm font-medium text-surface-800-200">Shadow model<select class="input mt-2" name="modelId" value={claudePolicy.model_id}>{#each anthropicModels as model (model.id)}<option value={model.id}>{model.name}</option>{/each}{#if !anthropicModels.length}<option value={claudePolicy.model_id}>{claudePolicy.model_id}</option>{/if}</select></label>
          <label class="block text-sm font-medium text-surface-800-200">Reasoning<select class="input mt-2" name="reasoningEffort" value={claudePolicy.reasoning_effort || "medium"}>{#each reasoningLevels as effort}<option value={effort}>{effort}</option>{/each}</select></label>
          <button class="btn min-h-10 preset-tonal-primary" type="submit" disabled={isPending("llm-claude-settings")} aria-busy={isPending("llm-claude-settings")}>{isPending("llm-claude-settings") ? "Saving…" : "Save settings"}</button>
        </form>
      {/if}
      <form class="mt-5 space-y-4" method="POST" action="?/saveLlmConnection" use:enhance={setPending("llm-connection")}>
        <input type="hidden" name="authType" value={connectionMethod === "claude" ? "claude_code" : "api_key"} />
        <div class="grid gap-4 sm:grid-cols-2"><label class="block text-sm font-medium text-surface-800-200">Connection name<input class="input mt-2" name="name" placeholder={connectionMethod === "claude" ? "Claude ranking" : "Primary ranking model"} required /></label><label class="block text-sm font-medium text-surface-800-200">Endpoint<span class="mt-1 block text-xs font-normal text-surface-700-300">Leave blank for provider defaults.</span><input class="input mt-2" name="endpoint" value={selectedProvider?.api ?? ""} placeholder="https://api.example.com/v1" /></label></div>
        <div class={`grid gap-4 ${connectionMethod === "claude" ? "sm:grid-cols-3" : "sm:grid-cols-2"}`}>
          <div class="relative">
            <label class="block text-sm font-medium text-surface-800-200" for="llm-provider-search">Search for provider</label>
            <input id="llm-provider-search" class="input mt-2" value={catalogQuery} onfocus={() => providerSearchOpen = true} oninput={(event) => { catalogQuery = event.currentTarget.value; providerSearchOpen = true; }} onkeydown={(event) => { if (event.key === "Escape") providerSearchOpen = false; }} placeholder={catalog ? "Search providers or models" : "Loading catalog…"} autocomplete="off" />
            {#if providerSearchOpen && catalog}
              <div class="absolute z-30 mt-2 max-h-80 w-full overflow-y-auto border border-surface-300-700 bg-surface-50-950 p-1 shadow-xl" role="listbox" aria-label="Matching providers and models">
                {#each matchingModels.slice(0, 40) as result (`${result.provider.id}::${result.model.id}`)}
                  <button type="button" class="flex w-full items-start gap-3 p-3 text-left hover:preset-tonal-primary" role="option" aria-selected={selectedProviderId === result.provider.id && selectedModelId === result.model.id} onclick={() => selectCatalogModel(result.provider, result.model)}>
                    <span class="min-w-0 flex-1"><strong class="block truncate text-sm text-surface-950-50">{result.provider.name}</strong><span class="mt-0.5 block truncate text-xs text-surface-700-300">{result.model.name}</span></span><span class="max-w-[45%] truncate text-right text-xs text-surface-600-400">{result.model.id}</span>
                  </button>
                {:else}<p class="p-3 text-sm text-surface-700-300">No providers or models match that search.</p>{/each}
                {#if matchingModels.length > 40}<p class="border-t border-surface-300-700/50 p-2 text-xs text-surface-600-400">Showing the first 40 matches. Refine your search for more.</p>{/if}
              </div>
            {/if}
            <span class="mt-1 block text-xs font-normal text-surface-700-300">Choose a matching provider/model from the popup.</span>
          </div>
          <label class="block text-sm font-medium text-surface-800-200">Selected model<input class="input mt-2" value={modelChoice} oninput={(event) => selectModel(event.currentTarget.value)} placeholder="provider::model-id" required /><span class="mt-1 block text-xs font-normal text-surface-700-300">Selected provider and model are saved together.</span></label>
          {#if connectionMethod === "claude"}<label class="block text-sm font-medium text-surface-800-200">Reasoning<select class="input mt-2" name="reasoningEffort" value={claudePolicy?.reasoning_effort ?? "medium"}>{#each reasoningLevels as effort}<option value={effort}>{effort}</option>{/each}</select></label>{/if}
        </div>
        <input type="hidden" name="providerId" value={selectedProviderId} /><input type="hidden" name="modelId" value={selectedModelId} />
        {#if connectionMethod === "api"}<label class="block text-sm font-medium text-surface-800-200">API key<input class="input mt-2" name="apiKey" type="password" autocomplete="new-password" placeholder="Stored securely by Bookward" required /></label>{:else}<label class="block text-sm font-medium text-surface-800-200">Claude Code OAuth token<input class="input mt-2" name="oauthToken" type="password" autocomplete="new-password" placeholder="Paste the token from Claude Code" required /></label>{/if}
        <button class="btn min-h-11 w-full preset-filled-secondary-500" type="submit" disabled={isPending("llm-connection")} aria-busy={isPending("llm-connection")}>{#if isPending("llm-connection")}<RefreshCw size={16} class="animate-spin" />{:else}<Check size={16} />{/if} Save connection and create shadow policy</button>
      </form>
    {/if}

    <div class="mt-6 border-t border-surface-300-700/50 pt-5"><div class="mb-3 flex items-center gap-2"><ShieldCheck size={17} class="text-tertiary-600-400" /><h4 class="text-sm font-semibold text-surface-950-50">Saved connections</h4></div><div class="space-y-3">{#each visibleConnections as connection (connection.id)}<div class="border border-surface-300-700/50 p-3"><div class="flex items-start gap-3"><div class="min-w-0 flex-1"><strong class="block truncate text-sm text-surface-950-50">{connection.name}</strong><span class="mt-1 block truncate text-xs text-surface-700-300">{connection.provider_id} · {connection.model_id}</span></div><span class={`badge shrink-0 ${connection.enabled ? "preset-tonal-success" : "preset-tonal-surface"}`}>{connection.enabled ? "Enabled" : "Disabled"}</span></div><div class="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-surface-700-300"><span>{authLabel(connection.auth_type)} · {connection.last_status ?? "Not used"}</span>{#if connection.enabled}<form method="POST" action="?/disableLlmConnection" use:enhance={setPending(`llm-disable-${connection.id}`, optimisticDisableConnection)}><input type="hidden" name="id" value={connection.id} /><button class="btn btn-sm min-h-8 preset-tonal-error" type="submit" disabled={isPending(`llm-disable-${connection.id}`)} aria-label={`Disable ${connection.name}`}><X size={14} /> Disable</button></form>{/if}</div></div>{:else}<p class="border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">No model connections yet.</p>{/each}</div></div>
  </section>

  <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="shadow-policy-heading"><div class="mb-5 flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"><ShieldCheck size={19} /></span><div><h3 id="shadow-policy-heading" class="text-lg font-semibold text-surface-950-50">Shadow policies</h3><p class="mt-1 max-w-2xl text-sm leading-6 text-surface-700-300">Each saved model gets one enabled shadow policy automatically. Shadow runs never change the books you see.</p></div></div>{#if visibleConnections.length === 0}<p class="border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">Connect a LLM above to create its shadow policy automatically.</p>{:else}<div class="space-y-3">{#each visiblePolicies as policy (policy.id)}<div class="border border-surface-300-700/50 p-4"><div class="flex flex-wrap items-center gap-3"><strong class="text-sm text-surface-950-50">{policy.name}</strong><span class={`badge ${policy.enabled ? "preset-tonal-success" : "preset-tonal-surface"}`}>{policy.enabled ? "Enabled" : "Disabled"}</span><span class="text-xs text-surface-700-300">{policy.connection_name} · top {policy.top_k}</span></div>{#if policy.auth_type === "openai_codex" || policy.auth_type === "claude_code"}<p class="mt-2 text-xs text-surface-700-300">Model and reasoning are configured in the connection picker above: {policy.model_id} · {policy.reasoning_effort || "medium"}.</p>{/if}<div class="mt-3 flex flex-wrap gap-2"><form method="POST" action="?/runLlmPolicy" use:enhance={setPending(`llm-run-${policy.id}`)}><input type="hidden" name="id" value={policy.id} /><button class="btn btn-sm min-h-9 preset-filled-tertiary-500" type="submit" disabled={!policy.enabled || isPending(`llm-run-${policy.id}`)} aria-busy={isPending(`llm-run-${policy.id}`)}><Play size={14} /> Run now</button></form>{#if policy.enabled}<form method="POST" action="?/disableLlmPolicy" use:enhance={setPending(`llm-policy-disable-${policy.id}`, optimisticDisablePolicy)}><input type="hidden" name="id" value={policy.id} /><button class="btn btn-sm min-h-9 preset-tonal-error" type="submit" disabled={isPending(`llm-policy-disable-${policy.id}`)}><X size={14} /> Disable</button></form>{/if}</div></div>{:else}<p class="mt-4 text-sm text-surface-700-300">No policies have been created yet.</p>{/each}</div>{/if}</section>

  <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="shadow-runs-heading"><div class="mb-5 flex flex-wrap items-start justify-between gap-4"><div class="flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-primary"><RefreshCw size={19} /></span><div><h3 id="shadow-runs-heading" class="text-lg font-semibold text-surface-950-50">Recent shadow runs</h3><p class="mt-1 text-sm text-surface-700-300">Status and timing only; model prompts and credentials are never shown.</p></div></div><form method="POST" action="?/refreshLlmRuns" use:enhance={setPending("llm-runs")}><button class="btn btn-sm min-h-10 preset-tonal-surface" type="submit" disabled={isPending("llm-runs")}><RefreshCw size={15} class={isPending("llm-runs") ? "animate-spin" : ""} /> Refresh</button></form></div>{#if runs.length}<div class="overflow-x-auto"><table class="table w-full text-left text-sm"><thead><tr class="border-b border-surface-300-700/50 text-xs uppercase tracking-wide text-surface-600-400"><th class="px-2 py-2">Status</th><th class="px-2 py-2">Created</th><th class="px-2 py-2">Candidates</th><th class="px-2 py-2">Latency</th></tr></thead><tbody>{#each runs as run (run.id)}<tr class="border-b border-surface-300-700/30"><td class="px-2 py-3"><span class={`badge ${run.status === "complete" ? "preset-tonal-success" : run.status === "failed" ? "preset-tonal-error" : "preset-tonal-secondary"}`}>{run.status}</span></td><td class="px-2 py-3 text-surface-700-300">{formatDate(run.created_at)}</td><td class="px-2 py-3 text-surface-700-300">{run.candidate_count}</td><td class="px-2 py-3 text-surface-700-300">{run.latency_ms == null ? "—" : `${run.latency_ms} ms`}</td></tr>{/each}</tbody></table></div>{:else}<p class="border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">No shadow runs yet. Your first run will appear here.</p>{/if}</section>
</section>
