<script lang="ts">
	import { fade, scale } from 'svelte/transition';
	import { BookOpen } from '@lucide/svelte';
	import type { MouseEventHandler } from 'svelte/elements';

	type Props = {
		title: string;
		score: number;
		coverUrl?: string | null;
		expanded?: boolean;
		loading?: 'lazy' | 'eager';
		id?: string;
		onclick?: MouseEventHandler<HTMLButtonElement>;
	};

	let {
		title,
		score,
		coverUrl = '',
		expanded = false,
		loading = 'lazy',
		id,
		onclick
	}: Props = $props();

	const fallbackId = $props.id();
	const baseId = $derived(id ?? fallbackId);
	const titleId = $derived(`${baseId}-title`);
	const actionId = $derived(`${baseId}-action`);

	function motionDuration(duration: number) {
		if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return duration;
		return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : duration;
	}
</script>

<div class="relative hidden h-full w-full self-stretch sm:block">
	<button
		type="button"
		class="relative block h-full w-full overflow-hidden preset-tonal-surface text-left focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 before:pointer-events-none before:absolute before:inset-0 before:z-10 before:bg-gradient-to-t before:from-primary-950/30 before:via-transparent before:to-secondary-500/10 before:content-['']"
		aria-labelledby={`${titleId} ${actionId}`}
		aria-expanded={expanded}
		{onclick}
	>
		<span class="absolute inset-0 flex flex-col items-center justify-center gap-3 p-4 text-center text-surface-600-400">
			<BookOpen size={28} aria-hidden="true" />
			<span id={titleId} class="line-clamp-3 text-xs">{title}</span>
		</span>
		<span id={actionId} class="sr-only">view details</span>
		{#if coverUrl}
			<img
				in:fade={{ duration: motionDuration(340) }}
				class="absolute inset-0 h-full w-full object-cover transition duration-500 group-hover:scale-[1.05]"
				src={coverUrl}
				alt=""
				{loading}
				onerror={(event) => ((event.currentTarget as HTMLImageElement).hidden = true)}
			/>
		{/if}
	</button>
	<span
		in:scale={{ duration: motionDuration(160) }}
		class="badge pointer-events-none absolute bottom-3 left-3 z-20 preset-filled-primary-500"
		title="Relative ranking score, not a probability or star rating"
	>Rank {score}</span>
</div>
