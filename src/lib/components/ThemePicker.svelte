<script lang="ts">
	import { onMount } from 'svelte';
	import { fade, fly, scale } from 'svelte/transition';
	import { Check, Monitor, Moon, Palette, Sun, X } from '@lucide/svelte';
	import ModalDialog from './ModalDialog.svelte';
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

	let {
		compact = false,
		open = $bindable(false),
	}: {
		compact?: boolean;
		open?: boolean;
	} = $props();

	const modes: { id: ThemeMode; label: string; description: string; icon: typeof Sun }[] = [
		{ id: 'system', label: 'System', description: 'Follow your device', icon: Monitor },
		{ id: 'light', label: 'Light', description: 'Bright and clear', icon: Sun },
		{ id: 'dark', label: 'Dark', description: 'Low light', icon: Moon },
	];

	let theme = $state<SkeletonThemeName>(DEFAULT_THEME);
	let mode = $state<ThemeMode>(DEFAULT_MODE);
	let appearanceButton = $state<HTMLButtonElement | null>(null);

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

	function closeAppearance(restoreFocus = true) {
		open = false;
		if (restoreFocus) appearanceButton?.focus({ preventScroll: true });
	}

	function openAppearance() {
		open = true;
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

<div class="relative min-w-0">
	<button
		bind:this={appearanceButton}
		type="button"
		class={compact ? 'btn min-h-11 w-full justify-start gap-3 preset-tonal-surface px-3' : 'btn-icon min-h-11 min-w-11 preset-tonal-surface'}
		aria-label="Choose appearance"
		title="Choose appearance"
		aria-expanded={open}
		aria-controls="appearance-panel"
		aria-haspopup="dialog"
		onclick={() => (open ? closeAppearance() : openAppearance())}
	>
		<Palette size={18} strokeWidth={1.8} aria-hidden="true" />
		{#if compact}<span>Appearance</span>{/if}
	</button>
	<ModalDialog
		id="appearance-panel"
		{open}
		ariaLabel="Appearance"
		onClose={() => closeAppearance()}
		class="w-[min(24rem,calc(100vw-2rem))] rounded-container border border-surface-200-800 preset-filled-surface-50-950 p-4 shadow-xl sm:p-5"
	>
		<div class="min-w-0 space-y-4">
			<div class="flex items-start justify-between gap-4">
				<div class="min-w-0">
					<h2 class="text-lg font-semibold">Appearance</h2>
					<p class="mt-1 text-sm text-surface-700-300">Make Bookward feel like yours.</p>
				</div>
				<button
					type="button"
					class="btn-icon btn-icon-sm shrink-0 preset-tonal-surface"
					aria-label="Close appearance picker"
					title="Close"
					data-modal-initial-focus
					onclick={() => closeAppearance()}
				>
					<X size={15} strokeWidth={1.8} aria-hidden="true" />
				</button>
			</div>

			<fieldset class="min-w-0 space-y-2 border-0 p-0">
				<legend class="text-xs font-semibold uppercase tracking-[0.12em] text-surface-700-300">Color mode</legend>
				<div class="grid grid-cols-3 gap-1 rounded-container preset-tonal-surface p-1" role="group" aria-label="Color mode">
					{#each modes as item}
						<button
							in:fly={{ y: 6, duration: motionDuration(180), delay: motionDuration(20) }}
							type="button"
							class={`btn min-h-11 min-w-0 flex-col gap-1 px-1 ${mode === item.id ? 'preset-filled-primary-500' : 'preset-tonal-surface'}`}
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
				<div class="grid grid-cols-2 gap-2" role="group" aria-label="Skeleton theme">
					{#each THEMES as item}
						<button
							in:fly={{ y: 6, duration: motionDuration(180), delay: motionDuration(20) }}
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
		</div>
	</ModalDialog>
</div>
