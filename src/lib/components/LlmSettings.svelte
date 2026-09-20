<script lang="ts">
  import { enhance } from "$app/forms";
  import type { SubmitFunction } from "@sveltejs/kit";
  import {
    Check,
    ExternalLink,
    KeyRound,
    Link2,
    LogOut,
    Play,
    RefreshCw,
    ShieldCheck,
    Sparkles,
    X,
  } from "@lucide/svelte";

  type ProviderModel = {
    id: string;
    name: string;
    family: string;
    reasoning: boolean;
    structured_output: boolean;
    tool_call: boolean;
    context: number;
    output: number;
    input_cost: number | null;
    output_cost: number | null;
  };
  type Provider = {
    id: string;
    name: string;
    api: string;
    models: ProviderModel[];
  };
  type Catalog = { providers?: Provider[]; fetched_at?: string; stale?: boolean };
  type DeviceLogin = {
    status?: string;
    authenticated?: boolean;
    login_id?: string;
    verification_url?: string;
    user_code?: string;
    account?: { email?: string | null; planType?: string | null; plan_type?: string | null };
    error?: string;
  };
  type Connection = {
    id: number;
    name: string;
    provider_id: string;
    model_id: string;
    endpoint: string;
    auth_type: string;
    enabled: number;
    last_status: string | null;
    last_used_at: string | null;
  };
  type Policy = {
    id: number;
    name: string;
    connection_id: number;
    enabled: number;
    top_k: number;
    prompt_version: string;
    connection_name: string;
    provider_id: string;
    model_id: string;
  };
  type Run = {
    id: string;
    policy_id: number;
    connection_id: number;
    status: string;
    candidate_count: number;
    latency_ms: number | null;
    created_at: string;
    finished_at: string | null;
  };
  type LlmData = {
    connections: Connection[];
    policies: Policy[];
    runs: Run[];
  };
  type FormState = {
    llmCatalog?: Catalog;
    deviceLogin?: DeviceLogin;
    llmRuns?: Run[];
  } | null | undefined;

  let {
    llm,
    formState,
    setPending,
    isPending,
  }: {
    llm: LlmData;
    formState: FormState;
    setPending: (key: string) => SubmitFunction;
    isPending: (key: string) => boolean;
  } = $props();

  let catalog = $state<Catalog | null>(null);
  let deviceLogin = $state<DeviceLogin | null>(null);
  let runs = $state<Run[]>([]);
  let catalogQuery = $state("");
  let selectedProviderId = $state("");
  let selectedModelId = $state("");
  let authType = $state("api_key");

  $effect(() => {
    runs = llm.runs;
  });
  $effect(() => {
    if (formState?.llmCatalog) catalog = formState.llmCatalog;
    if (formState?.deviceLogin) deviceLogin = formState.deviceLogin;
    if (formState?.llmRuns) runs = formState.llmRuns;
  });
  $effect(() => {
    if (!selectedProviderId && catalog?.providers?.length) {
      selectedProviderId = catalog.providers[0].id;
      selectedModelId = catalog.providers[0].models[0]?.id ?? "";
    }
  });

  const filteredProviders = $derived(
    (catalog?.providers ?? [])
      .map((provider) => ({
        ...provider,
        models: provider.models.filter((model) => {
          const query = catalogQuery.trim().toLowerCase();
          return !query || `${provider.name} ${provider.id} ${model.name} ${model.id}`.toLowerCase().includes(query);
        }),
      }))
      .filter((provider) => provider.models.length),
  );
  const selectedProvider = $derived(
    catalog?.providers?.find((provider) => provider.id === selectedProviderId) ?? null,
  );
  const anthropicConnections = $derived(
    llm.connections.filter((connection) => connection.provider_id.toLowerCase() === "anthropic"),
  );

  function selectProvider(provider: Provider) {
    selectedProviderId = provider.id;
    selectedModelId = provider.models[0]?.id ?? "";
  }
  function formatDate(value: string | null | undefined) {
    if (!value) return "—";
    const parsed = new Date(value.replace(" ", "T"));
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }
  function authLabel(value: string) {
    return value === "openai_codex" ? "ChatGPT subscription" : value === "claude_code" ? "Claude Code OAuth" : "API key";
  }
</script>

<section class="mt-6 space-y-5 lg:col-span-2" aria-labelledby="llm-settings-heading">
  <div class="max-w-3xl">
    <p class="mb-2 text-xs font-semibold uppercase tracking-[0.18em] text-primary-600-400">Model connections</p>
    <h2 id="llm-settings-heading" class="text-2xl font-semibold text-surface-950-50">LLM settings</h2>
    <p class="mt-2 text-sm leading-6 text-surface-700-300">
      Connect a hosted model or a subscription runner for optional shadow ranking. API keys and OAuth tokens are stored by the engine and are never returned to this page.
    </p>
  </div>

  <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="llm-catalog-heading">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div class="flex min-w-0 items-start gap-3">
        <span class="grid size-10 shrink-0 place-items-center preset-tonal-primary"><Sparkles size={19} /></span>
        <div>
          <h3 id="llm-catalog-heading" class="text-lg font-semibold text-surface-950-50">Models.dev catalog</h3>
          <p class="mt-1 text-sm leading-6 text-surface-700-300">Browse current text models and choose a provider without entering a secret in the browser URL.</p>
        </div>
      </div>
      <form method="POST" action="?/loadLlmCatalog" use:enhance={setPending("llm-catalog")}>
        <label class="sr-only" for="llm-catalog-refresh">Refresh model catalog</label>
        <input id="llm-catalog-refresh" type="hidden" name="refresh" value="on" />
        <button class="btn btn-sm min-h-10 preset-tonal-primary" type="submit" disabled={isPending("llm-catalog")} aria-busy={isPending("llm-catalog")}>
          {#if isPending("llm-catalog")}<RefreshCw size={15} class="animate-spin" />{:else}<RefreshCw size={15} />{/if} {catalog ? "Refresh catalog" : "Load catalog"}
        </button>
      </form>
    </div>
    {#if catalog}
      <div class="mt-5 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
        <label class="block text-sm font-medium text-surface-800-200">Search providers and models<input class="input mt-2" bind:value={catalogQuery} placeholder="OpenAI, Claude, reasoning…" /></label>
        <div class="self-end text-xs text-surface-700-300 sm:text-right">
          <span class="block">{filteredProviders.reduce((count, provider) => count + provider.models.length, 0)} text models</span>
          <span class="mt-1 block">{catalog.stale ? "Cached catalog · refresh unavailable" : `Fetched ${formatDate(catalog.fetched_at)}`}</span>
        </div>
      </div>
      <div class="mt-4 grid max-h-72 gap-2 overflow-y-auto pr-1 sm:grid-cols-2">
        {#each filteredProviders as provider (provider.id)}
          <div class="border border-surface-300-700/50 p-3">
            <button type="button" class="flex w-full items-center justify-between gap-3 text-left" onclick={() => selectProvider(provider)}>
              <span class="min-w-0"><strong class="block truncate text-sm text-surface-950-50">{provider.name}</strong><small class="block truncate text-xs text-surface-700-300">{provider.id} · {provider.models.length} models</small></span>
              <span class="text-xs text-primary-600-400">Use</span>
            </button>
            <div class="mt-2 flex flex-wrap gap-1.5">{#each provider.models.slice(0, 5) as model (model.id)}<button type="button" class={`badge max-w-full truncate ${selectedModelId === model.id && selectedProviderId === provider.id ? "preset-filled-primary-500" : "preset-tonal-surface"}`} title={model.name} onclick={() => { selectedProviderId = provider.id; selectedModelId = model.id; }}>{model.name}</button>{/each}{#if provider.models.length > 5}<span class="badge preset-tonal-surface">+{provider.models.length - 5}</span>{/if}</div>
          </div>
        {:else}<p class="py-5 text-sm text-surface-700-300">No catalog models match that search.</p>{/each}
      </div>
    {:else}<div class="mt-5 border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">Load the catalog to browse providers and model capabilities.</div>{/if}
  </section>

  <div class="grid gap-5 lg:grid-cols-2">
    <form class="card preset-tonal-surface p-5 sm:p-6" method="POST" action="?/saveLlmConnection" use:enhance={setPending("llm-connection")}>
      <div class="mb-5 flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-secondary"><Link2 size={19} /></span><div><h3 class="text-lg font-semibold text-surface-950-50">Add a model connection</h3><p class="mt-1 text-sm text-surface-700-300">Use an API key, ChatGPT subscription, or Claude Code OAuth.</p></div></div>
      <div class="space-y-4">
        <label class="block text-sm font-medium text-surface-800-200">Connection name<input class="input mt-2" name="name" placeholder="Primary ranking model" required /></label>
        <div class="grid gap-4 sm:grid-cols-2">
          <label class="block text-sm font-medium text-surface-800-200">Provider<input class="input mt-2" name="providerId" value={selectedProviderId} placeholder="openai or anthropic" required /></label>
          <label class="block text-sm font-medium text-surface-800-200">Model<input class="input mt-2" name="modelId" value={selectedModelId} placeholder="gpt-4.1-mini" required /></label>
        </div>
        <label class="block text-sm font-medium text-surface-800-200">Authentication<select class="select mt-2" name="authType" bind:value={authType}><option value="api_key">API key / OpenAI-compatible</option><option value="openai_codex">ChatGPT subscription</option><option value="claude_code">Claude Code OAuth</option></select></label>
        {#if authType === "api_key"}<label class="block text-sm font-medium text-surface-800-200">API key<input class="input mt-2" name="apiKey" type="password" autocomplete="new-password" placeholder="Stored securely by Bookward" /></label>{:else if authType === "claude_code"}<label class="block text-sm font-medium text-surface-800-200">Claude OAuth token<input class="input mt-2" name="oauthToken" type="password" autocomplete="new-password" placeholder="Paste the token from Claude Code" /></label>{:else}<p class="border border-secondary-500/30 preset-tonal-secondary p-3 text-sm text-surface-800-200">This connection uses the ChatGPT device login below. No API key is needed.</p>{/if}
        <label class="block text-sm font-medium text-surface-800-200">Endpoint<span class="mt-1 block text-xs font-normal text-surface-700-300">Leave blank for OpenAI or Anthropic defaults.</span><input class="input mt-2" name="endpoint" value={selectedProvider?.api ?? ""} placeholder="https://api.example.com/v1" /></label>
      </div>
      <button class="btn mt-5 min-h-11 w-full preset-filled-secondary-500" type="submit" disabled={isPending("llm-connection")} aria-busy={isPending("llm-connection")}>{#if isPending("llm-connection")}<RefreshCw size={16} class="animate-spin" />{:else}<Check size={16} />{/if} Save connection</button>
    </form>

    <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="llm-connections-heading">
      <div class="mb-5 flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"><ShieldCheck size={19} /></span><div><h3 id="llm-connections-heading" class="text-lg font-semibold text-surface-950-50">Saved connections</h3><p class="mt-1 text-sm text-surface-700-300">Only connection metadata and health are shown here.</p></div></div>
      <div class="space-y-3">
        {#each llm.connections as connection (connection.id)}
          <div class="border border-surface-300-700/50 p-3">
            <div class="flex items-start gap-3"><div class="min-w-0 flex-1"><strong class="block truncate text-sm text-surface-950-50">{connection.name}</strong><span class="mt-1 block truncate text-xs text-surface-700-300">{connection.provider_id} · {connection.model_id}</span></div><span class={`badge shrink-0 ${connection.enabled ? "preset-tonal-success" : "preset-tonal-surface"}`}>{connection.enabled ? "Enabled" : "Disabled"}</span></div>
            <div class="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-surface-700-300"><span>{authLabel(connection.auth_type)} · {connection.last_status ?? "Not used"}</span>{#if connection.enabled}<form method="POST" action="?/disableLlmConnection" use:enhance={setPending(`llm-disable-${connection.id}`)}><input type="hidden" name="id" value={connection.id} /><button class="btn btn-sm min-h-8 preset-tonal-error" type="submit" disabled={isPending(`llm-disable-${connection.id}`)} aria-label={`Disable ${connection.name}`}><X size={14} /> Disable</button></form>{/if}</div>
          </div>
        {:else}<p class="border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">No model connections yet.</p>{/each}
      </div>
    </section>
  </div>

  <div class="grid gap-5 lg:grid-cols-2">
    <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="chatgpt-login-heading">
      <div class="mb-5 flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-primary"><KeyRound size={19} /></span><div><h3 id="chatgpt-login-heading" class="text-lg font-semibold text-surface-950-50">ChatGPT device login</h3><p class="mt-1 text-sm leading-6 text-surface-700-300">Sign in through the official device flow for subscription-backed OpenAI models.</p></div></div>
      <div class="border border-surface-300-700/50 p-4 text-sm" role="status">
        {#if deviceLogin?.status === "pending"}<p class="font-semibold text-surface-950-50">Waiting for authorization</p><p class="mt-1 text-surface-700-300">Code: <strong class="text-surface-950-50">{deviceLogin.user_code || "—"}</strong></p>{#if deviceLogin.verification_url}<a class="mt-3 inline-flex items-center gap-2 text-primary-600-400 underline" href={deviceLogin.verification_url} target="_blank" rel="noreferrer">Open verification page <ExternalLink size={14} /></a>{/if}{:else if deviceLogin?.authenticated}<p class="font-semibold text-success-600-400">Signed in{deviceLogin.account?.email ? ` as ${deviceLogin.account.email}` : ""}.</p>{:else if deviceLogin?.status === "failed"}<p class="font-semibold text-error-600-400">Device login failed.</p>{:else if deviceLogin?.status === "cancelled"}<p class="text-surface-700-300">Device login cancelled.</p>{:else}<p class="text-surface-700-300">No ChatGPT subscription is connected.</p>{/if}
      </div>
      <div class="mt-4 flex flex-wrap gap-2">
        {#if deviceLogin?.status === "pending"}<form method="POST" action="?/openaiDeviceLoginCancel" use:enhance={setPending("openai-cancel")}><input type="hidden" name="loginId" value={deviceLogin.login_id ?? ""} /><button class="btn btn-sm min-h-10 preset-tonal-error" type="submit" disabled={isPending("openai-cancel")}><X size={15} /> Cancel</button></form>{:else}<form method="POST" action="?/openaiDeviceLoginStart" use:enhance={setPending("openai-start")}><button class="btn btn-sm min-h-10 preset-filled-primary-500" type="submit" disabled={isPending("openai-start")} aria-busy={isPending("openai-start")}>{#if isPending("openai-start")}<RefreshCw size={15} class="animate-spin" />{:else}<KeyRound size={15} />{/if} Start device login</button></form>{/if}
        <form method="POST" action="?/openaiDeviceLoginStatus" use:enhance={setPending("openai-status")}><button class="btn btn-sm min-h-10 preset-tonal-surface" type="submit" disabled={isPending("openai-status")}><RefreshCw size={15} class={isPending("openai-status") ? "animate-spin" : ""} /> Check status</button></form>
        <form method="POST" action="?/openaiDeviceLogout" use:enhance={setPending("openai-logout")}><button class="btn btn-sm min-h-10 preset-tonal-surface" type="submit" disabled={isPending("openai-logout")}><LogOut size={15} /> Sign out</button></form>
      </div>
    </section>

    <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="claude-oauth-heading">
      <div class="mb-5 flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-secondary"><KeyRound size={19} /></span><div><h3 id="claude-oauth-heading" class="text-lg font-semibold text-surface-950-50">Claude Code OAuth</h3><p class="mt-1 text-sm leading-6 text-surface-700-300">Attach a Claude Code OAuth token to an existing Anthropic connection.</p></div></div>
      {#if anthropicConnections.length}<form method="POST" action="?/saveClaudeOAuth" use:enhance={setPending("claude-oauth")} class="space-y-4"><label class="block text-sm font-medium text-surface-800-200">Anthropic connection<select class="select mt-2" name="connectionId">{#each anthropicConnections as connection (connection.id)}<option value={connection.id}>{connection.name} · {connection.model_id}</option>{/each}</select></label><label class="block text-sm font-medium text-surface-800-200">OAuth token<input class="input mt-2" name="oauthToken" type="password" autocomplete="new-password" placeholder="Paste token; it is never displayed again" required /></label><button class="btn min-h-11 w-full preset-filled-secondary-500" type="submit" disabled={isPending("claude-oauth")} aria-busy={isPending("claude-oauth")}>{#if isPending("claude-oauth")}<RefreshCw size={16} class="animate-spin" />{:else}<Check size={16} />{/if} Save OAuth token</button></form>{:else}<p class="border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">Create an Anthropic connection first, then attach its Claude Code token here.</p>{/if}
    </section>
  </div>

  <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="shadow-policy-heading">
    <div class="mb-5 flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-tertiary"><ShieldCheck size={19} /></span><div><h3 id="shadow-policy-heading" class="text-lg font-semibold text-surface-950-50">Shadow policies</h3><p class="mt-1 max-w-2xl text-sm leading-6 text-surface-700-300">Run a model alongside the visible recommender to compare signals. Shadow runs never change the books you see.</p></div></div>
    {#if llm.connections.length}<form method="POST" action="?/saveLlmPolicy" use:enhance={setPending("llm-policy")} class="grid gap-4 border border-surface-300-700/50 p-4 md:grid-cols-4"><label class="block text-sm font-medium text-surface-800-200 md:col-span-2">Policy name<input class="input mt-2" name="name" placeholder="OpenAI shadow ranking" required /></label><label class="block text-sm font-medium text-surface-800-200">Connection<select class="select mt-2" name="connectionId">{#each llm.connections as connection (connection.id)}<option value={connection.id}>{connection.name}</option>{/each}</select></label><label class="block text-sm font-medium text-surface-800-200">Candidates<input class="input mt-2" name="topK" type="number" min="1" max="100" value="20" required /></label><input type="hidden" name="promptVersion" value="shadow-v1" /><button class="btn min-h-11 preset-filled-tertiary-500 md:col-span-4" type="submit" disabled={isPending("llm-policy")} aria-busy={isPending("llm-policy")}>{#if isPending("llm-policy")}<RefreshCw size={16} class="animate-spin" />{:else}<Check size={16} />{/if} Save shadow policy</button></form>{:else}<p class="border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">Add a model connection before creating a shadow policy.</p>{/if}
    <div class="mt-4 space-y-3">{#each llm.policies as policy (policy.id)}<div class="border border-surface-300-700/50 p-4"><div class="flex flex-wrap items-center gap-3"><strong class="text-sm text-surface-950-50">{policy.name}</strong><span class={`badge ${policy.enabled ? "preset-tonal-success" : "preset-tonal-surface"}`}>{policy.enabled ? "Enabled" : "Disabled"}</span><span class="text-xs text-surface-700-300">{policy.connection_name} · top {policy.top_k}</span></div><div class="mt-3 flex flex-wrap gap-2"><form method="POST" action="?/runLlmPolicy" use:enhance={setPending(`llm-run-${policy.id}`)}><input type="hidden" name="id" value={policy.id} /><button class="btn btn-sm min-h-9 preset-filled-tertiary-500" type="submit" disabled={!policy.enabled || isPending(`llm-run-${policy.id}`)} aria-busy={isPending(`llm-run-${policy.id}`)}><Play size={14} /> Run now</button></form>{#if policy.enabled}<form method="POST" action="?/disableLlmPolicy" use:enhance={setPending(`llm-policy-disable-${policy.id}`)}><input type="hidden" name="id" value={policy.id} /><button class="btn btn-sm min-h-9 preset-tonal-error" type="submit" disabled={isPending(`llm-policy-disable-${policy.id}`)}><X size={14} /> Disable</button></form>{/if}</div></div>{:else}<p class="mt-4 text-sm text-surface-700-300">No shadow policies yet.</p>{/each}</div>
  </section>

  <section class="card preset-tonal-surface p-5 sm:p-6" aria-labelledby="shadow-runs-heading">
    <div class="mb-5 flex flex-wrap items-start justify-between gap-4"><div class="flex items-start gap-3"><span class="grid size-10 shrink-0 place-items-center preset-tonal-primary"><RefreshCw size={19} /></span><div><h3 id="shadow-runs-heading" class="text-lg font-semibold text-surface-950-50">Recent shadow runs</h3><p class="mt-1 text-sm text-surface-700-300">Status and timing only; model prompts and credentials are never shown.</p></div></div><form method="POST" action="?/refreshLlmRuns" use:enhance={setPending("llm-runs")}><button class="btn btn-sm min-h-10 preset-tonal-surface" type="submit" disabled={isPending("llm-runs")}><RefreshCw size={15} class={isPending("llm-runs") ? "animate-spin" : ""} /> Refresh</button></form></div>
    {#if runs.length}<div class="overflow-x-auto"><table class="table w-full text-left text-sm"><thead><tr class="border-b border-surface-300-700/50 text-xs uppercase tracking-wide text-surface-600-400"><th class="px-2 py-2">Status</th><th class="px-2 py-2">Created</th><th class="px-2 py-2">Candidates</th><th class="px-2 py-2">Latency</th></tr></thead><tbody>{#each runs as run (run.id)}<tr class="border-b border-surface-300-700/30"><td class="px-2 py-3"><span class={`badge ${run.status === "complete" ? "preset-tonal-success" : run.status === "failed" ? "preset-tonal-error" : "preset-tonal-secondary"}`}>{run.status}</span></td><td class="px-2 py-3 text-surface-700-300">{formatDate(run.created_at)}</td><td class="px-2 py-3 text-surface-700-300">{run.candidate_count}</td><td class="px-2 py-3 text-surface-700-300">{run.latency_ms == null ? "—" : `${run.latency_ms} ms`}</td></tr>{/each}</tbody></table></div>{:else}<p class="border border-dashed border-surface-300-700/50 p-4 text-sm text-surface-700-300">No shadow runs yet. Run a policy when you are ready to compare a model.</p>{/if}
  </section>
</section>
