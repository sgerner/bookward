<script lang="ts">
	import { tick, type Snippet } from 'svelte';

	const FOCUSABLE_SELECTOR = [
		'a[href]',
		'button:not(:disabled)',
		'input:not(:disabled):not([type="hidden"])',
		'select:not(:disabled)',
		'textarea:not(:disabled)',
		'[tabindex]:not([tabindex="-1"])',
	].join(', ');

	let {
		open = $bindable(false),
		panelId,
		triggerLabel,
		triggerTitle,
		triggerClass = 'btn btn-icon min-h-11 min-w-11 preset-tonal-surface',
		dialogLabel,
		dialogLabelledby,
		panelClass = '',
		class: className = 'relative',
		trigger,
		children,
	}: {
		open?: boolean;
		panelId?: string;
		triggerLabel?: string;
		triggerTitle?: string;
		triggerClass?: string;
		dialogLabel?: string;
		dialogLabelledby?: string;
		panelClass?: string;
		class?: string;
		trigger: Snippet;
		children: Snippet<[{ close: (restoreFocus?: boolean) => void; panelId: string }]>;
	} = $props();

	const uid = $props.id();
	const resolvedPanelId = $derived(panelId ?? `${uid}-panel`);

	let rootEl = $state<HTMLDivElement | null>(null);
	let triggerButton = $state<HTMLButtonElement | null>(null);
	let panelEl = $state<HTMLDivElement | null>(null);
	let pointerDownInside = false;

	function focusFirstControl() {
		if (!panelEl) return;
		const candidates = panelEl.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
		for (const element of candidates) {
			if (!element.closest('[hidden]')) {
				element.focus();
				return;
			}
		}
		panelEl.focus();
	}

	function close(restoreFocus = true) {
		if (restoreFocus) triggerButton?.focus();
		open = false;
	}

	async function openPopover() {
		open = true;
		await tick();
		focusFirstControl();
	}

	function toggle() {
		if (open) {
			close(true);
		} else {
			void openPopover();
		}
	}

	function handleFocusOut(event: FocusEvent) {
		if (!open || !rootEl) return;
		const nextTarget = event.relatedTarget;
		if (nextTarget instanceof Node) {
			if (!rootEl.contains(nextTarget)) {
				close(false);
			}
			return;
		}
		queueMicrotask(() => {
			if (!open || pointerDownInside || !rootEl) return;
			const active = typeof document !== 'undefined' ? document.activeElement : null;
			if (!active || !rootEl.contains(active)) {
				close(false);
			}
		});
	}

	$effect(() => {
		if (!open || typeof window === 'undefined') return;

		const onKeydown = (event: KeyboardEvent) => {
			if (event.key !== 'Escape' || !open || !rootEl) return;
			if (rootEl.querySelector('[data-popover-root][data-popover-open="true"]')) return;
			event.preventDefault();
			event.stopPropagation();
			close(true);
		};

		const onPointerDown = (event: PointerEvent) => {
			const target = event.target;
			if (!open || !rootEl || !(target instanceof Node)) return;
			if (rootEl.contains(target)) {
				pointerDownInside = true;
				queueMicrotask(() => {
					pointerDownInside = false;
				});
			} else {
				close(false);
			}
		};

		window.addEventListener('keydown', onKeydown, true);
		document.addEventListener('pointerdown', onPointerDown, true);

		return () => {
			window.removeEventListener('keydown', onKeydown, true);
			document.removeEventListener('pointerdown', onPointerDown, true);
		};
	});
</script>

<div
	bind:this={rootEl}
	class={className}
	data-popover-root
	data-popover-open={open ? 'true' : 'false'}
	onfocusout={handleFocusOut}
>
	<button
		bind:this={triggerButton}
		type="button"
		class={triggerClass}
		aria-label={triggerLabel}
		title={triggerTitle}
		aria-haspopup="dialog"
		aria-expanded={open}
		aria-controls={resolvedPanelId}
		onclick={toggle}
	>
		{@render trigger()}
	</button>
	<div
		bind:this={panelEl}
		id={resolvedPanelId}
		role="dialog"
		aria-label={dialogLabel}
		aria-labelledby={dialogLabelledby}
		tabindex="-1"
		hidden={!open}
		class={`popover-panel ${panelClass}`.trim()}
	>
		{@render children({ close, panelId: resolvedPanelId })}
	</div>
</div>

<style>
	.popover-panel[hidden] {
		display: none;
	}

	.popover-panel {
		transition: opacity 150ms ease;
	}

	@starting-style {
		.popover-panel:not([hidden]) {
			opacity: 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.popover-panel {
			transition: none;
		}
	}
</style>
