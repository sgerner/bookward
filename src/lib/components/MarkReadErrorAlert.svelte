<script lang="ts">
	import { TriangleAlert } from '@lucide/svelte';
	import { fly } from 'svelte/transition';

	let {
		message = '',
		transitionDuration = 180
	}: {
		message?: string;
		transitionDuration?: number;
	} = $props();
</script>

{#if message}
	<div
		in:fly={{ y: -8, duration: transitionDuration }}
		class="mt-5 flex items-start gap-2 rounded-xl border border-error-500/30 preset-tonal-error p-3 text-sm"
		role="alert"
	>
		<TriangleAlert size={16} class="mt-0.5 shrink-0" aria-hidden="true" />
		<span>{message}</span>
	</div>
{/if}
