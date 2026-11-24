<script module lang="ts">
	/**
	 * Shared accessible popover dialog primitive (`role="dialog"` with mounted `[hidden]`
	 * panel so `aria-controls` never dangles).
	 *
	 * When `topLayer` is enabled (e.g., header menus or compact nested popovers inside
	 * `backdrop-filter` / `z-40` chrome), opening promotes the panel into the browser's
	 * native HTML Popover API top layer (`popover="manual"`, Baseline 2024: Chrome/Edge 114+,
	 * Safari 17+, Firefox 125+).
	 *
	 * Fallback: In browsers without `HTMLElement.prototype.showPopover`, `syncPopoverTopLayer`
	 * removes `popover`, sets `[data-popover-fallback]`, and portals the panel to
	 * `document.body` while open, restoring it to `rootEl` on close so `aria-controls` stays
	 * connected.
	 */
	function isNativePopoverOpen(node: HTMLElement): boolean {
		try {
			return typeof node.matches === 'function' && node.matches(':popover-open');
		} catch {
			return false;
		}
	}

	export function syncPopoverTopLayer(
		panel: HTMLElement,
		root: HTMLElement | null,
	): () => void {
		const supportsPopover = typeof panel.showPopover === 'function';
		let portaledToBody = false;

		if (supportsPopover) {
			panel.setAttribute?.('popover', 'manual');
			if (!isNativePopoverOpen(panel)) {
				panel.showPopover();
			}
		} else {
			panel.removeAttribute?.('popover');
			panel.setAttribute?.('data-popover-fallback', '');
			if (
				typeof document !== 'undefined' &&
				document.body &&
				typeof document.body.appendChild === 'function' &&
				panel.parentNode !== document.body
			) {
				document.body.appendChild(panel);
				portaledToBody = true;
			}
		}

		panel.scrollTop = 0;

		return () => {
			if (supportsPopover) {
				if (isNativePopoverOpen(panel) && typeof panel.hidePopover === 'function') {
					panel.hidePopover();
				}
				panel.removeAttribute?.('popover');
			} else {
				panel.removeAttribute?.('data-popover-fallback');
				if (portaledToBody) {
					if (root?.isConnected && typeof root.appendChild === 'function') {
						root.appendChild(panel);
					} else if (typeof panel.remove === 'function') {
						panel.remove();
					}
				}
			}
		};
	}
</script>

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

	const NESTED_OPEN_OVERLAY_SELECTOR =
		'[data-popover-root][data-popover-open="true"], dialog[open]';

	let {
		open = $bindable(false),
		topLayer = false,
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
		topLayer?: boolean;
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

	function containsNode(node: Node): boolean {
		return Boolean(rootEl?.contains(node) || panelEl?.contains(node));
	}

	function hasNestedOpenOverlay(): boolean {
		return Boolean(
			rootEl?.querySelector(NESTED_OPEN_OVERLAY_SELECTOR) ||
				panelEl?.querySelector(NESTED_OPEN_OVERLAY_SELECTOR),
		);
	}

	function focusFirstControl() {
		if (!panelEl) return;
		panelEl.scrollTop = 0;
		const candidates = panelEl.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
		for (const element of candidates) {
			if (!element.closest('[hidden]')) {
				element.focus({ preventScroll: true });
				return;
			}
		}
		panelEl.focus({ preventScroll: true });
	}

	function close(restoreFocus = true) {
		open = false;
		if (restoreFocus) triggerButton?.focus({ preventScroll: true });
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
		if (!open || !rootEl || hasNestedOpenOverlay()) return;
		const nextTarget = event.relatedTarget;
		if (nextTarget instanceof Node) {
			if (!containsNode(nextTarget)) {
				close(false);
			}
			return;
		}
		queueMicrotask(() => {
			if (!open || pointerDownInside || !rootEl || hasNestedOpenOverlay()) return;
			const active = typeof document !== 'undefined' ? document.activeElement : null;
			if (!active || !containsNode(active)) {
				close(false);
			}
		});
	}

	function handlePanelFocusOut(event: FocusEvent) {
		if (typeof document !== 'undefined' && panelEl?.parentElement === document.body) {
			handleFocusOut(event);
		}
	}

	$effect(() => {
		if (!open || typeof window === 'undefined') return;

		const cleanupTopLayer =
			topLayer && panelEl ? syncPopoverTopLayer(panelEl, rootEl) : undefined;

		const onKeydown = (event: KeyboardEvent) => {
			if (
				event.key !== 'Escape' ||
				event.defaultPrevented ||
				event.isComposing ||
				!open ||
				!rootEl
			) {
				return;
			}
			if (hasNestedOpenOverlay()) return;
			event.preventDefault();
			event.stopPropagation();
			close(true);
		};

		const onPointerDown = (event: PointerEvent) => {
			const target = event.target;
			if (!open || !rootEl || !(target instanceof Node)) return;
			if (hasNestedOpenOverlay()) return;
			if (containsNode(target)) {
				pointerDownInside = true;
				queueMicrotask(() => {
					pointerDownInside = false;
				});
			} else {
				close(false);
			}
		};

		window.addEventListener('keydown', onKeydown);
		document.addEventListener('pointerdown', onPointerDown, true);

		return () => {
			window.removeEventListener('keydown', onKeydown);
			document.removeEventListener('pointerdown', onPointerDown, true);
			cleanupTopLayer?.();
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
		onfocusout={handlePanelFocusOut}
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
