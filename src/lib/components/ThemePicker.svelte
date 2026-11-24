<script lang="ts">
	import { onMount } from 'svelte';
	import { fade, scale } from 'svelte/transition';
	import { Check, Monitor, Moon, Palette, Sun, X } from '@lucide/svelte';
	import PopoverDialog from './PopoverDialog.svelte';
	import RadioGroup from '#lib/components/RadioGroup.svelte';
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
	let headerRowEl = $state<HTMLDivElement | null>(null);

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

	$effect(() => {
		if (typeof window === 'undefined' || !headerRowEl) return;
		const panelEl = headerRowEl.closest<HTMLElement>('[role="dialog"]');
		if (!panelEl) return;
		if (!compact || !appearanceOpen) {
			panelEl.style.removeProperty('top');
			panelEl.style.removeProperty('max-height');
			return;
		}
		const siteHeaderEl = headerRowEl.closest('header');
		if (!siteHeaderEl) return;

		const syncBounds = () => {
			const headerRect = siteHeaderEl.getBoundingClientRect();
			const topOffset = Math.max(80, Math.ceil(Math.max(0, headerRect.bottom) + 12));
			const availableHeight = Math.max(160, Math.floor(window.innerHeight - topOffset - 16));
			panelEl.style.top = `${topOffset}px`;
			panelEl.style.maxHeight = `min(42rem, ${availableHeight}px)`;
		};

		syncBounds();
		const observer =
			typeof ResizeObserver !== 'undefined' ? new ResizeObserver(syncBounds) : null;
		observer?.observe(siteHeaderEl);
		window.addEventListener('resize', syncBounds);
		return () => {
			observer?.disconnect();
			window.removeEventListener('resize', syncBounds);
			panelEl.style.removeProperty('top');
			panelEl.style.removeProperty('max-height');
		};
	});

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
	triggerClass={compact ? 'btn btn-reflow min-h-11 w-full justify-start gap-3 preset-tonal-surface px-3 text-left' : 'btn-icon min-h-11 min-w-11 preset-tonal-surface'}
	triggerLabel="Choose appearance"
	triggerTitle="Choose appearance"
	dialogLabelledby={headingId}
	panelClass={`modal-scroll-shell ${compact ? 'fixed right-4 top-20 max-h-[min(42rem,calc(100vh-6rem))] max-h-[min(42rem,calc(100dvh-6rem))]' : 'absolute right-0 top-[calc(100%+0.75rem)] max-h-[min(42rem,calc(100vh-2rem))] max-h-[min(42rem,calc(100dvh-2rem))]'} z-50 w-[min(24rem,calc(100vw-2rem))] preset-filled-surface-50-950 shadow-xl`}
>
	{#snippet trigger()}
		<Palette size={18} strokeWidth={1.8} aria-hidden="true" />
		{#if compact}<span>Appearance</span>{/if}
	{/snippet}
	{#snippet children({ close })}
		<div bind:this={headerRowEl} class="flex shrink-0 items-start justify-between gap-4 border-b border-surface-300-700/40 p-4 pb-3 sm:p-5 sm:pb-4">
			<div class="min-w-0">
				<h2 id={headingId} class="text-lg font-semibold">Appearance</h2>
				<p class="text-reflow mt-1 text-sm text-surface-700-300">Make Bookward feel like yours.</p>
			</div>
			<button type="button" class="btn-icon btn-icon-sm min-h-9 min-w-9 shrink-0 preset-tonal-surface" aria-label="Close appearance picker" title="Close" onclick={() => close()}><X size={15} strokeWidth={1.8} aria-hidden="true" /></button>
		</div>

		<div class="modal-scroll-body space-y-4 p-4 pt-3 sm:p-5 sm:pt-4">
			<fieldset class="min-w-0 space-y-2 border-0 p-0">
				<legend id="appearance-color-mode-legend" class="text-xs font-semibold uppercase tracking-[0.12em] text-surface-700-300">Color mode</legend>
				<RadioGroup
					items={modes}
					value={mode}
					getValue={(entry) => entry.id}
					labelledBy="appearance-color-mode-legend"
					columns={3}
					class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,5.5em),1fr))] gap-1.5 rounded-container border border-[var(--control-border)] preset-tonal-surface p-1.5"
					onSelect={(entry) => chooseMode(entry.id)}
				>
					{#snippet item(entry, radio)}
						<button
							type="button"
							role="radio"
							aria-checked={radio.checked}
							tabindex={radio.tabIndex}
							data-active={radio.checked ? 'true' : 'false'}
							class={`theme-picker-option btn btn-reflow min-h-11 flex-col gap-1 px-2 ${radio.checked ? 'preset-filled-primary-500' : 'preset-tonal-surface'}`}
							title={entry.description}
							onclick={radio.select}
						>
							<entry.icon size={15} strokeWidth={1.8} aria-hidden="true" />
							<span class="text-reflow text-xs">{entry.label}</span>
						</button>
					{/snippet}
				</RadioGroup>
			</fieldset>

			<fieldset class="min-w-0 space-y-2 border-0 p-0">
				<legend id="appearance-theme-legend" class="text-xs font-semibold uppercase tracking-[0.12em] text-surface-700-300">Theme</legend>
				<div class="flex items-center justify-between gap-3">
					<span class="text-xs text-surface-700-300">{THEMES.length} palettes</span>
				</div>
				<RadioGroup
					items={THEMES}
					value={theme}
					getValue={(entry) => entry.id}
					labelledBy="appearance-theme-legend"
					columns={2}
					class="-mx-1.5 grid grid-cols-[repeat(auto-fit,minmax(min(100%,10em),1fr))] gap-2 p-1.5"
					onSelect={(entry) => void chooseTheme(entry.id)}
				>
					{#snippet item(entry, radio)}
						<button
							type="button"
							role="radio"
							aria-checked={radio.checked}
							tabindex={radio.tabIndex}
							data-active={radio.checked ? 'true' : 'false'}
							class={`theme-picker-option flex min-h-11 min-w-0 items-center gap-2 rounded-container p-2 text-left text-sm transition-colors ${radio.checked ? 'preset-filled-primary-500' : 'preset-tonal-surface hover:preset-tonal-primary'}`}
							aria-label={`Use ${entry.label} theme`}
							onclick={radio.select}
						>
							<span class="grid shrink-0 grid-cols-3 overflow-hidden rounded-container ring-1 ring-[var(--control-border)]" aria-hidden="true">
								<span class="size-3" style={`background-color: ${THEME_SWATCHES[entry.id].primary}`}></span>
								<span class="size-3" style={`background-color: ${THEME_SWATCHES[entry.id].secondary}`}></span>
								<span class="size-3" style={`background-color: ${THEME_SWATCHES[entry.id].tertiary}`}></span>
							</span>
							<span class="text-reflow flex-1">{entry.label}</span>
							{#if radio.checked}<span in:scale={{ duration: motionDuration(150) }} out:fade={{ duration: motionDuration(90) }} class="ms-auto shrink-0"><Check size={15} strokeWidth={2.2} aria-hidden="true" /></span>{/if}
						</button>
					{/snippet}
				</RadioGroup>
			</fieldset>
		</div>
	{/snippet}
</PopoverDialog>

<style>
	.theme-picker-option {
		border: 1px solid var(--control-border);
	}

	.theme-picker-option[data-active='true'] {
		border-color: var(--control-border-strong);
	}

	.theme-picker-option:focus-visible {
		position: relative;
		z-index: 1;
		outline: 2px solid var(--focus-ring-outer);
		outline-offset: 2px;
		box-shadow:
			0 0 0 2px var(--focus-ring-inner),
			0 0 0 4px var(--focus-ring-outer);
	}

	.theme-picker-option[data-active='true']:focus-visible {
		box-shadow:
			0 0 0 2px var(--focus-ring-swatch-inner),
			0 0 0 4px var(--focus-ring-outer);
	}

	@media (forced-colors: active) {
		.theme-picker-option[data-active='true'] {
			border: 2px solid Highlight;
		}

		.theme-picker-option:focus-visible {
			outline: 2px solid Highlight;
			outline-offset: 2px;
		}
	}
</style>
