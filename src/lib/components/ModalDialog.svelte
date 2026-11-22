<script module lang="ts">
	import { modalFocus } from '#lib/actions/modal-focus.js';

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

	export function attachModalDialog(
		node: HTMLDialogElement,
		initialOptions: ModalDialogActionOptions = {},
	) {
		let options = initialOptions;
		const previousFocus = typeof document !== 'undefined' ? document.activeElement : null;

		if (typeof node.showModal === 'function') {
			if (node.open && typeof node.close === 'function') {
				node.close();
			}
			node.showModal();
		} else {
			node.setAttribute('open', '');
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

		const handleKeydown = (event: KeyboardEvent) => {
			if (event.key !== 'Escape') return;
			event.preventDefault();
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

		node.addEventListener('cancel', handleCancel);
		node.addEventListener('click', handleClick);
		if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
			window.addEventListener('keydown', handleKeydown, true);
		} else {
			node.addEventListener('keydown', handleKeydown);
		}

		return {
			update(nextOptions: ModalDialogActionOptions = {}) {
				options = nextOptions;
			},
			destroy() {
				node.removeEventListener('cancel', handleCancel);
				node.removeEventListener('click', handleClick);
				if (typeof window !== 'undefined' && typeof window.removeEventListener === 'function') {
					window.removeEventListener('keydown', handleKeydown, true);
				} else {
					node.removeEventListener('keydown', handleKeydown);
				}
				focusTrap?.destroy();
				if (node.open && typeof node.close === 'function') {
					node.close();
				}
				if (
					options.restoreFocus !== false &&
					isFocusTarget(previousFocus) &&
					previousFocus.isConnected !== false
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
		className.includes('max-h-') ? '' : 'max-h-[calc(100dvh-2rem)]',
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
		class={`m-auto ${maxHeightClass} min-w-0 overflow-x-hidden overflow-y-auto backdrop:bg-surface-950/75 backdrop:backdrop-blur-sm ${className}`.replace(/\s+/g, ' ').trim()}
	>
		{@render children()}
	</dialog>
{/if}
