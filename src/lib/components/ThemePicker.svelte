<script lang="ts">
	import { onMount } from 'svelte';
	import { fade, scale } from 'svelte/transition';
	import { Check, Monitor, Moon, Palette, Sun, X } from '@lucide/svelte';
	import PopoverDialog from './PopoverDialog.svelte';
	import {
		DEFAULT_MODE,
		DEFAULT_THEME,
		THEMES,
		THEME_SWATCHES,
		applyTheme,
		getStoredPreferences,
		loadTheme,
		persistPreferences,
		type SkeletonThemeName,
		type ThemeMode,
	} from '#lib/theme.js';

	let { compact = false }: { compact?: boolean } = $props();

	const headingId = $props.id();

	const modes: { id: ThemeMode; label: string; description: string; icon: typeof Sun }[] = [
		{ id: 'system', label: 'System', description: 'Follow your device', icon: Monitor },
		{ id: 'light', label: 'Light', description: 'Bright and clear', icon: Sun },
		{ id: 'dark', label: 'Dark', description: 'Low light', icon: Moon },
	];

	let theme = $state<SkeletonThemeName>(DEFAULT_THEME);
	let mode = $state<ThemeMode>(DEFAULT_MODE);
	let appearanceOpen = $state(false);

	async function chooseTheme(nextTheme: SkeletonThemeName) {
		try {
			await loadTheme(nextTheme);
		} catch {
			return;
		}
		theme = nextTheme;
		persistPreferences(theme, mode);
		applyTheme(theme, mode);
	}

	function chooseMode(nextMode: ThemeMode) {
		mode = nextMode;
		persistPreferences(theme, mode);
		applyTheme(theme, mode);
	}

	function motionDuration(duration: number) {
		if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return duration;
		return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : duration;
	}

	onMount(() => {
		const sync = async () => {
			const stored = getStoredPreferences();
			try {
				await loadTheme(stored.theme);
			} catch {
				return;
			}
			theme = stored.theme;
			mode = stored.mode;
			applyTheme(theme, mode);
		};
		void sync();
		window.addEventListener('storage', sync);
		return () => {
			window.removeEventListener('storage', sync);
		};
	});
</script>

<PopoverDialog
	bind:open={appearanceOpen}
	triggerClass={compact ? 'btn min-h-11 w-full justify-start gap-3 preset-tonal-surface px-3' : 'btn-icon min-h-11 min-w-11 preset-tonal-surface'}
	triggerLabel="Choose appearance"
	triggerTitle="Choose appearance"
	dialogLabelledby={headingId}
	panelClass={`${compact ? 'fixed right-4 top-20' : 'absolute right-0 top-[calc(100%+0.75rem)]'} z-50 max-h-[min(42rem,calc(100dvh-2rem))] w-[min(24rem,calc(100vw-2rem))] overflow-y-auto preset-filled-surface-50-950 p-4 shadow-xl sm:p-5`}
>
	{#snippet trigger()}
		<Palette size={18} strokeWidth={1.8} aria-hidden="true" />
		{#if compact}<span>Appearance</span>{/if}
	{/snippet}
	{#snippet children({ close })}
		<div class="flex items-start justify-between gap-4">
			<div>
				<h2 id={headingId} class="text-lg font-semibold">Appearance</h2>
				<p class="mt-1 text-sm text-surface-700-300">Make Bookward feel like yours.</p>
			</div>
			<button type="button" class="btn-icon btn-icon-sm preset-tonal-surface" aria-label="Close appearance picker" title="Close" onclick={() => close()}><X size={15} strokeWidth={1.8} aria-hidden="true" /></button>
		</div>

		<fieldset class="min-w-0 space-y-2 border-0 p-0">
			<legend class="text-xs font-semibold uppercase tracking-[0.12em] text-surface-700-300">Color mode</legend>
			<div class="grid grid-cols-3 gap-1 rounded-container preset-tonal-surface p-1" role="group" aria-label="Color mode">
				{#each modes as item}
					<button
						type="button"
						class={`btn min-h-11 flex-col gap-1 ${mode === item.id ? 'preset-filled-primary-500' : 'preset-tonal-surface'}`}
						aria-pressed={mode === item.id}
						title={item.description}
						onclick={() => chooseMode(item.id)}
					>
						<item.icon size={15} strokeWidth={1.8} aria-hidden="true" />
						<span class="text-xs">{item.label}</span>
					</button>
				{/each}
			</div>
		</fieldset>

		<fieldset class="min-w-0 space-y-2 border-0 p-0">
			<legend class="text-xs font-semibold uppercase tracking-[0.12em] text-surface-700-300">Theme</legend>
			<div class="flex items-center justify-between gap-3">
				<span class="text-xs text-surface-700-300">{THEMES.length} palettes</span>
			</div>
			<div class="grid max-h-[min(22rem,45vh)] grid-cols-2 gap-2 overflow-y-auto pr-1" role="group" aria-label="Skeleton theme">
				{#each THEMES as item}
					<button
						type="button"
						class={`flex min-h-11 min-w-0 items-center gap-2 rounded-container p-2 text-left text-sm transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary-500 ${theme === item.id ? 'preset-filled-primary-500' : 'preset-tonal-surface hover:preset-tonal-primary'}`}
						aria-pressed={theme === item.id}
						aria-label={`Use ${item.label} theme`}
						onclick={() => chooseTheme(item.id)}
					>
						<span class="grid shrink-0 grid-cols-3 overflow-hidden rounded-container ring-1 ring-surface-300-700" aria-hidden="true">
							<span class="size-3" style={`background-color: ${THEME_SWATCHES[item.id].primary}`}></span>
							<span class="size-3" style={`background-color: ${THEME_SWATCHES[item.id].secondary}`}></span>
							<span class="size-3" style={`background-color: ${THEME_SWATCHES[item.id].tertiary}`}></span>
						</span>
						<span class="min-w-0 flex-1 truncate">{item.label}</span>
						{#if theme === item.id}<span in:scale={{ duration: motionDuration(150) }} out:fade={{ duration: motionDuration(90) }} class="ms-auto shrink-0"><Check size={15} strokeWidth={2.2} aria-hidden="true" /></span>{/if}
					</button>
				{/each}
			</div>
		</fieldset>
	{/snippet}
</PopoverDialog>
