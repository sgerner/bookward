<script lang="ts">
	import type { Snippet } from 'svelte';
	import { fade, scale } from 'svelte/transition';
	import { BookOpen } from '@lucide/svelte';
	import type { MouseEventHandler } from 'svelte/elements';

	type Props = {
		title: string;
		score: number;
		coverUrl?: string | null;
		hasCover?: boolean;
		expanded?: boolean;
		loading?: 'lazy' | 'eager';
		id?: string;
		class?: string;
		onclick?: MouseEventHandler<HTMLButtonElement>;
		children?: Snippet;
	};

	let {
		title,
		score,
		coverUrl = '',
		hasCover,
		expanded = false,
		loading = 'lazy',
		id,
		class: className = '',
		onclick,
		children
	}: Props = $props();

	const fallbackId = $props.id();
	const baseId = $derived(id ?? fallbackId);
	const titleId = $derived(`${baseId}-title`);
	const actionId = $derived(`${baseId}-action`);
	const showMobileBackdrop = $derived(hasCover ?? Boolean(coverUrl));

	function motionDuration(duration: number) {
		if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return duration;
		return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : duration;
	}
</script>

<div
	class={`${showMobileBackdrop ? 'pointer-events-none absolute inset-0 z-0' : 'hidden'} aspect-[2/3] h-full w-full self-start overflow-hidden rounded-[inherit] sm:pointer-events-auto sm:relative sm:inset-auto sm:z-auto sm:block sm:h-auto sm:rounded-none sm:rounded-tl-[inherit] sm:preset-tonal-surface sm:before:pointer-events-none sm:before:absolute sm:before:inset-0 sm:before:z-10 sm:before:bg-gradient-to-t sm:before:from-primary-950/30 sm:before:via-transparent sm:before:to-secondary-500/10 sm:before:content-[''] ${className}`.trim()}
>
	<span
		class="pointer-events-none absolute inset-0 hidden flex-col items-center justify-center gap-3 p-4 text-center text-surface-600-400 sm:flex"
		aria-hidden="true"
	>
		<BookOpen size={28} aria-hidden="true" />
		<span id={titleId} class="line-clamp-3 text-xs">{title}</span>
	</span>
	{#if children}
		{@render children()}
	{:else if coverUrl}
		<img
			in:fade={{ duration: motionDuration(340) }}
			class="absolute inset-0 h-full w-full scale-105 object-cover opacity-75 blur-[2px] sm:scale-100 sm:opacity-100 sm:blur-none sm:transition sm:duration-500 sm:group-hover:scale-[1.05]"
			src={coverUrl}
			alt=""
			width="160"
			height="240"
			{loading}
			onerror={(event) => ((event.currentTarget as HTMLImageElement).hidden = true)}
		/>
	{/if}
	<div
		class="pointer-events-none absolute inset-0 preset-filled-surface-50-950 opacity-[0.82] sm:hidden"
		aria-hidden="true"
	></div>
	<div
		class="pointer-events-none absolute inset-0 bg-gradient-to-br from-primary-500/10 via-transparent to-secondary-500/15 sm:hidden"
		aria-hidden="true"
	></div>
	<button
		type="button"
		class="hidden text-left focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 sm:absolute sm:inset-0 sm:z-20 sm:block"
		aria-labelledby={`${titleId} ${actionId}`}
		aria-expanded={expanded}
		{onclick}
	>
		<span id={actionId} class="sr-only">view details</span>
	</button>
	<span class="pointer-events-none absolute bottom-3 left-3 z-20 hidden sm:block">
		<span
			in:scale={{ duration: motionDuration(160) }}
			class="badge pointer-events-none preset-filled-primary-500"
			title="Relative ranking score, not a probability or star rating"
		>Rank {score}</span>
	</span>
</div>
