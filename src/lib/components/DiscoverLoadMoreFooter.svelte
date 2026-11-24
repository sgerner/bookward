<script lang="ts">
	import { ArrowRight, RefreshCw } from '@lucide/svelte';
	import { discoverSentinel } from '#lib/actions/discover-sentinel.js';

	let {
		observeEnabled = true,
		loading = false,
		error = '',
		resetKey = undefined,
		onLoadMore
	}: {
		observeEnabled?: boolean;
		loading?: boolean;
		error?: string;
		resetKey?: string | number;
		onLoadMore: () => void;
	} = $props();
</script>

<div
	use:discoverSentinel={{
		enabled: observeEnabled,
		loading,
		error,
		resetKey,
		onIntersect: onLoadMore
	}}
	class="col-span-full flex flex-col items-center gap-2 pt-1"
>
	{#if error}
		<p class="text-sm text-error-600-400" role="alert">{error}</p>
	{/if}
	<button
		type="button"
		class="btn btn-sm preset-tonal-secondary"
		onclick={onLoadMore}
		disabled={loading}
		aria-busy={loading}
	>
		{#if loading}
			<RefreshCw size={14} class="animate-spin" aria-hidden="true" />
		{:else if error}
			<RefreshCw size={14} aria-hidden="true" />
		{:else}
			<ArrowRight size={14} aria-hidden="true" />
		{/if}
		{error ? 'Try again' : 'Load more'}
	</button>
</div>
