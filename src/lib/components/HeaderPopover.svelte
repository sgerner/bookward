<script module lang="ts">
	export type HeaderPopoverActionOptions = {
		onClose?: (restoreFocus?: boolean) => void;
		trigger?: HTMLElement | null;
	};

	function isPopoverOpen(node: HTMLElement): boolean {
		try {
			return typeof node.matches === 'function' && node.matches(':popover-open');
		} catch {
			return false;
		}
	}

	function hasOpenNestedDialog(node: HTMLElement): boolean {
		return typeof node.querySelector === 'function' && Boolean(node.querySelector('dialog[open]'));
	}

	export function attachHeaderPopover(
		node: HTMLElement,
		initialOptions: HeaderPopoverActionOptions = {},
	) {
		let options = initialOptions;

		if (!isPopoverOpen(node) && typeof node.showPopover === 'function') {
			node.showPopover();
		}

		const handlePointerDown = (event: PointerEvent) => {
			if (!options.onClose || hasOpenNestedDialog(node)) return;
			const target = event.target;
			if (!target || typeof node.contains !== 'function') return;
			if (node.contains(target as Node)) return;
			if (
				options.trigger &&
				typeof options.trigger.contains === 'function' &&
				options.trigger.contains(target as Node)
			) {
				return;
			}
			options.onClose(false);
		};

		const handleKeydown = (event: KeyboardEvent) => {
			if (event.key !== 'Escape' || !options.onClose || hasOpenNestedDialog(node)) return;
			event.preventDefault();
			event.stopPropagation();
			options.onClose(true);
		};

		if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
			document.addEventListener('pointerdown', handlePointerDown);
		}
		if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
			window.addEventListener('keydown', handleKeydown);
		}

		return {
			update(nextOptions: HeaderPopoverActionOptions = {}) {
				options = nextOptions;
			},
			destroy() {
				if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') {
					document.removeEventListener('pointerdown', handlePointerDown);
				}
				if (typeof window !== 'undefined' && typeof window.removeEventListener === 'function') {
					window.removeEventListener('keydown', handleKeydown);
				}
				if (isPopoverOpen(node) && typeof node.hidePopover === 'function') {
					node.hidePopover();
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
		trigger = null,
		onClose = undefined,
		class: className = '',
		children,
	}: {
		id?: string;
		open?: boolean;
		ariaLabel?: string;
		trigger?: HTMLElement | null;
		onClose?: (restoreFocus?: boolean) => void;
		class?: string;
		children: Snippet;
	} = $props();
</script>

{#if open}
	<div
		use:attachHeaderPopover={{ onClose, trigger }}
		{id}
		popover="manual"
		aria-label={ariaLabel}
		class={`fixed right-4 top-[4.25rem] left-auto bottom-auto m-0 max-h-[calc(100dvh-4.5rem)] min-w-0 overflow-y-auto ${className}`.trim()}
	>
		{@render children()}
	</div>
{/if}
