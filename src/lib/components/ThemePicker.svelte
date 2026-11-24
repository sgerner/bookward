<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { fade, fly, scale } from 'svelte/transition';
	import { Check, Monitor, Moon, Palette, Sun, X } from '@lucide/svelte';
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

	const modes: { id: ThemeMode; label: string; description: string; icon: typeof Sun }[] = [
		{ id: 'system', label: 'System', description: 'Follow your device', icon: Monitor },
		{ id: 'light', label: 'Light', description: 'Bright and clear', icon: Sun },
		{ id: 'dark', label: 'Dark', description: 'Low light', icon: Moon },
	];

	let theme = $state<SkeletonThemeName>(DEFAULT_THEME);
	let mode = $state<ThemeMode>(DEFAULT_MODE);
	let appearanceOpen = $state(false);
	let compactStyle = $state('');
	let pickerRoot = $state<HTMLDivElement | null>(null);
	let appearanceButton = $state<HTMLButtonElement | null>(null);
	let closeButton = $state<HTMLButtonElement | null>(null);

	function updateCompactBounds() {
		if (!compact || !pickerRoot || typeof window === 'undefined') {
			compactStyle = '';
			return;
		}
		const headerEl = pickerRoot.closest('header');
		if (!headerEl) {
			compactStyle = '';
			return;
		}
		const headerRect = headerEl.getBoundingClientRect();
		const topOffset = Math.max(80, Math.ceil(headerRect.height + 12));
		const availableHeight = Math.max(
			160,
			Math.floor(window.innerHeight - Math.max(0, headerRect.top) - topOffset - 16),
		);
		compactStyle = `top: ${topOffset}px; max-height: min(42rem, ${availableHeight}px);`;
	}

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
		appearanceOpen = false;
		compactStyle = '';
		if (restoreFocus) appearanceButton?.focus();
	}

	async function openAppearance() {
		appearanceOpen = true;
		updateCompactBounds();
		await tick();
		updateCompactBounds();
		closeButton?.focus();
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
		const onKeydown = (event: KeyboardEvent) => {
			if (event.key === 'Escape' && appearanceOpen) {
				event.stopPropagation();
				closeAppearance();
			}
		};
		const onPointerDown = (event: PointerEvent) => {
			const target = event.target;
			if (appearanceOpen && pickerRoot && target instanceof Node && !pickerRoot.contains(target)) closeAppearance(false);
		};
		const onResize = () => {
			if (appearanceOpen && compact) updateCompactBounds();
		};
		const headerEl = pickerRoot?.closest('header');
		const headerObserver =
			typeof ResizeObserver !== 'undefined' && headerEl
				? new ResizeObserver(() => {
						if (appearanceOpen && compact) updateCompactBounds();
					})
				: null;
		if (headerEl) headerObserver?.observe(headerEl);
		window.addEventListener('resize', onResize);
		window.addEventListener('keydown', onKeydown, true);
		document.addEventListener('pointerdown', onPointerDown);
		return () => {
			headerObserver?.disconnect();
			window.removeEventListener('resize', onResize);
			window.removeEventListener('storage', sync);
			window.removeEventListener('keydown', onKeydown, true);
			document.removeEventListener('pointerdown', onPointerDown);
		};
	});
</script>


<div bind:this={pickerRoot} class="relative">
	<button
		bind:this={appearanceButton}
		type="button"
		class={compact ? 'btn min-h-11 w-full justify-start gap-3 preset-tonal-surface px-3' : 'btn-icon min-h-11 min-w-11 preset-tonal-surface'}
		aria-label="Choose appearance"
		title="Choose appearance"
		aria-expanded={appearanceOpen}
		aria-controls="appearance-panel"
		aria-haspopup="dialog"
		onclick={() => appearanceOpen ? closeAppearance() : void openAppearance()}
	>
		<Palette size={18} strokeWidth={1.8} aria-hidden="true" />
		{#if compact}<span>Appearance</span>{/if}
	</button>
	{#if appearanceOpen}
		<div
			id="appearance-panel"
			role="dialog"
			aria-label="Appearance"
			in:fly={{ y: -10, duration: motionDuration(260) }}
			out:fade={{ duration: motionDuration(180) }}
			class={`modal-scroll-shell ${compact ? 'fixed right-4 top-20 max-h-[min(42rem,calc(100vh-6rem))] max-h-[min(42rem,calc(100dvh-6rem))]' : 'absolute right-0 top-[calc(100%+0.75rem)] max-h-[min(42rem,calc(100vh-2rem))] max-h-[min(42rem,calc(100dvh-2rem))]'} z-50 w-[min(24rem,calc(100vw-2rem))] preset-filled-surface-50-950 shadow-xl`}
			style={compact ? compactStyle : undefined}
		>
			<div class="flex shrink-0 items-start justify-between gap-4 border-b border-surface-300-700/40 p-4 pb-3 sm:p-5 sm:pb-4">
				<div class="min-w-0">
					<h2 class="text-reflow text-lg font-semibold">Appearance</h2>
					<p class="text-reflow mt-1 text-sm text-surface-700-300">Make Bookward feel like yours.</p>
				</div>
				<button bind:this={closeButton} type="button" class="btn-icon btn-icon-sm shrink-0 preset-tonal-surface" aria-label="Close appearance picker" title="Close" onclick={() => closeAppearance()}><X size={15} strokeWidth={1.8} aria-hidden="true" /></button>
			</div>

			<div class="modal-scroll-body space-y-4 p-4 pt-3 sm:p-5 sm:pt-4">
				<fieldset class="min-w-0 space-y-2 border-0 p-0">
					<legend class="text-xs font-semibold uppercase tracking-[0.12em] text-surface-700-300">Color mode</legend>
					<div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,5.5em),1fr))] gap-1 rounded-container preset-tonal-surface p-1" role="group" aria-label="Color mode">
						{#each modes as item}
							<button
								in:fly={{ y: 6, duration: motionDuration(180), delay: motionDuration(20) }}
								type="button"
								class={`btn min-h-11 flex-col gap-1 px-2 ${mode === item.id ? 'preset-filled-primary-500' : 'preset-tonal-surface'}`}
								aria-pressed={mode === item.id}
								title={item.description}
								onclick={() => chooseMode(item.id)}
							>
								<item.icon size={15} strokeWidth={1.8} aria-hidden="true" />
								<span class="text-reflow text-xs">{item.label}</span>
							</button>
						{/each}
					</div>
				</fieldset>

				<fieldset class="min-w-0 space-y-2 border-0 p-0">
					<legend class="text-xs font-semibold uppercase tracking-[0.12em] text-surface-700-300">Theme</legend>
					<div class="flex items-center justify-between gap-3">
						<span class="text-xs text-surface-700-300">{THEMES.length} palettes</span>
					</div>
					<div class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,10em),1fr))] gap-2" role="group" aria-label="Skeleton theme">
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
								<span class="text-reflow flex-1">{item.label}</span>
								{#if theme === item.id}<span in:scale={{ duration: motionDuration(150) }} out:fade={{ duration: motionDuration(90) }} class="ms-auto shrink-0"><Check size={15} strokeWidth={2.2} aria-hidden="true" /></span>{/if}
							</button>
						{/each}
					</div>
				</fieldset>
			</div>
		</div>
	{/if}
</div>
