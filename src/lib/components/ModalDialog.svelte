<script module lang="ts">
	import { modalFocus } from '#lib/actions/modal-focus.js';

	/**
	 * Native `<dialog>` modal primitive promoted to the browser Top Layer via
	 * `HTMLDialogElement.prototype.showModal()` (Baseline Widely Available:
	 * Chrome 37+, Edge 79+, Firefox 98+, Safari 15.4+).
	 *
	 * Fallback: In environments without `showModal()`, sets `[open]`,
	 * `aria-modal="true"`, and `[data-modal-fallback]`, applying viewport-fixed
	 * `z-[60]` positioning, a full-viewport backdrop shadow, outside-pointerdown
	 * dismissal, and bubble-phase `Escape` handling scoped to the topmost dialog.
	 */
	export type ModalDialogActionOptions = {
		onClose?: () => void;
		closeOnBackdrop?: boolean;
		restoreFocus?: boolean;
	};

	type FocusTarget = {
		focus: (options?: { preventScroll?: boolean }) => void;
		isConnected?: boolean;
	};

	function isFocusTarget(value: unknown): value is FocusTarget {
		return Boolean(value && typeof value === 'object' && typeof (value as FocusTarget).focus === 'function');
	}

	function isTopmostOpenDialog(node: HTMLDialogElement): boolean {
		if (typeof document === 'undefined' || typeof document.querySelectorAll !== 'function') {
			return true;
		}
		const openDialogs = document.querySelectorAll('dialog[open]');
		return openDialogs.length === 0 || openDialogs[openDialogs.length - 1] === node;
	}

	export function attachModalDialog(
		node: HTMLDialogElement,
		initialOptions: ModalDialogActionOptions = {},
	) {
		let options = initialOptions;
		const previousFocus = typeof document !== 'undefined' ? document.activeElement : null;
		const usedShowModal = typeof node.showModal === 'function';

		if (usedShowModal) {
			if (node.open && typeof node.close === 'function') {
				node.close();
			}
			node.showModal();
		} else {
			node.setAttribute('open', '');
			node.setAttribute('aria-modal', 'true');
			node.setAttribute('data-modal-fallback', '');
		}

		const focusTrap =
			typeof document !== 'undefined' &&
			typeof HTMLElement !== 'undefined' &&
			typeof getComputedStyle === 'function'
				? modalFocus(node)
				: null;
		node.scrollTop = 0;

		const handleCancel = (event: Event) => {
			event.preventDefault();
			options.onClose?.();
		};

		const handleFallbackKeydown = (event: KeyboardEvent) => {
			if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
			if (!isTopmostOpenDialog(node)) return;
			event.stopPropagation();
			options.onClose?.();
		};

		const handleClick = (event: MouseEvent) => {
			if (options.closeOnBackdrop === false) return;
			if (event.target !== node || event.detail === 0) return;
			const rect = node.getBoundingClientRect();
			const inside =
				rect.top <= event.clientY &&
				event.clientY <= rect.bottom &&
				rect.left <= event.clientX &&
				event.clientX <= rect.right;
			if (!inside) {
				options.onClose?.();
			}
		};

		const handleFallbackPointerDown = (event: PointerEvent) => {
			if (options.closeOnBackdrop === false || !isTopmostOpenDialog(node)) return;
			const target = event.target;
			if (!target || typeof node.contains !== 'function') return;
			if (node.contains(target as Node)) return;
			options.onClose?.();
		};

		node.addEventListener('cancel', handleCancel);
		node.addEventListener('click', handleClick);
		if (!usedShowModal) {
			node.addEventListener('keydown', handleFallbackKeydown);
			if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
				document.addEventListener('pointerdown', handleFallbackPointerDown);
			}
		}

		return {
			update(nextOptions: ModalDialogActionOptions = {}) {
				options = nextOptions;
			},
			destroy() {
				node.removeEventListener('cancel', handleCancel);
				node.removeEventListener('click', handleClick);
				if (!usedShowModal) {
					node.removeEventListener('keydown', handleFallbackKeydown);
					if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') {
						document.removeEventListener('pointerdown', handleFallbackPointerDown);
					}
				}
				focusTrap?.destroy();
				if (node.open && typeof node.close === 'function') {
					node.close();
				}
				const currentActive = typeof document !== 'undefined' ? document.activeElement : null;
				if (
					options.restoreFocus !== false &&
					isFocusTarget(previousFocus) &&
					previousFocus.isConnected !== false &&
					currentActive !== previousFocus
				) {
					previousFocus.focus({ preventScroll: true });
				}
			},
		};
	}
</script>

<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		id = undefined,
		open = false,
		ariaLabel = undefined,
		ariaLabelledby = undefined,
		ariaDescribedby = undefined,
		closeOnBackdrop = true,
		restoreFocus = true,
		onClose = undefined,
		class: className = '',
		children,
	}: {
		id?: string;
		open?: boolean;
		ariaLabel?: string;
		ariaLabelledby?: string;
		ariaDescribedby?: string;
		closeOnBackdrop?: boolean;
		restoreFocus?: boolean;
		onClose?: () => void;
		class?: string;
		children: Snippet;
	} = $props();

	const maxHeightClass = $derived(
		/(?:^|\s)max-h-/.test(className) ? '' : 'max-h-[calc(100dvh-2rem)]',
	);
	const overflowYClass = $derived(
		/(?:^|\s)overflow-y-/.test(className) ? '' : 'overflow-y-auto',
	);
</script>

{#if open}
	<dialog
		use:attachModalDialog={{ onClose, closeOnBackdrop, restoreFocus }}
		{id}
		aria-label={ariaLabel}
		aria-labelledby={ariaLabelledby}
		aria-describedby={ariaDescribedby}
		tabindex="-1"
		class={`m-auto ${maxHeightClass} min-w-0 overflow-x-hidden ${overflowYClass} backdrop:bg-surface-950/75 backdrop:backdrop-blur-sm data-[modal-fallback]:fixed data-[modal-fallback]:inset-0 data-[modal-fallback]:z-[60] data-[modal-fallback]:shadow-[0_0_0_100vmax_rgba(9,9,11,0.75)] ${className}`.replace(/\s+/g, ' ').trim()}
	>
		{@render children()}
	</dialog>
{/if}
