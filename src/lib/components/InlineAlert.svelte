<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		message = '',
		tone = 'error',
		size = 'md',
		class: className = '',
		children,
	}: {
		message?: string | null;
		tone?: 'error' | 'warning';
		size?: 'sm' | 'md';
		class?: string;
		children?: Snippet;
	} = $props();

	const trimmedMessage = $derived(message?.trim() ?? '');
	const toneClass = $derived(
		tone === 'warning' ? 'preset-tonal-warning' : 'preset-tonal-error',
	);
	const sizeClass = $derived(size === 'sm' ? 'p-3 text-xs' : 'p-4 text-sm');
</script>

{#if children}
	<div class={`rounded-xl ${sizeClass} ${toneClass} ${className}`.trim()} role="alert">
		{@render children()}
	</div>
{:else if trimmedMessage}
	<p class={`rounded-xl ${sizeClass} ${toneClass} ${className}`.trim()} role="alert">{trimmedMessage}</p>
{/if}
