<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		message = '',
		tone = 'error',
		class: className = '',
		children,
	}: {
		message?: string | null;
		tone?: 'error' | 'warning';
		class?: string;
		children?: Snippet;
	} = $props();

	const trimmedMessage = $derived(message?.trim() ?? '');
	const toneClass = $derived(
		tone === 'warning' ? 'preset-tonal-warning' : 'preset-tonal-error',
	);
</script>

{#if children}
	<div class={`rounded-xl p-4 text-sm ${toneClass} ${className}`.trim()} role="alert">
		{@render children()}
	</div>
{:else if trimmedMessage}
	<p class={`rounded-xl p-4 text-sm ${toneClass} ${className}`.trim()} role="alert">{trimmedMessage}</p>
{/if}
