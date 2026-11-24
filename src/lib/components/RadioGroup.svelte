<script module lang="ts">
	export type RadioItemState = {
		checked: boolean;
		tabIndex: 0 | -1;
		select: () => void;
	};
</script>

<script lang="ts" generics="Item">
	import type { Snippet } from 'svelte';
	import { rovingFocus } from '#lib/actions/roving-focus.js';

	let {
		items,
		value = null,
		getValue = (entry: Item) => entry as unknown as string | number,
		labelledBy,
		ariaLabel,
		columns = 1,
		selectOnMove = false,
		clearable = false,
		class: className = '',
		onSelect,
		onClear,
		item: renderItem,
	}: {
		items: readonly Item[];
		value?: string | number | null;
		getValue?: (entry: Item) => string | number;
		labelledBy?: string;
		ariaLabel?: string;
		columns?: number;
		selectOnMove?: boolean;
		clearable?: boolean;
		class?: string;
		onSelect?: (entry: Item) => void;
		onClear?: () => void;
		item: Snippet<[Item, RadioItemState, number]>;
	} = $props();

	let root = $state<HTMLDivElement | null>(null);
	let focusedIndex = $state<number | null>(null);

	const selectedIndex = $derived(
		value === null || value === undefined
			? -1
			: items.findIndex((entry) => getValue(entry) === value),
	);

	const activeIndex = $derived.by(() => {
		if (items.length === 0) return -1;
		if (focusedIndex !== null && focusedIndex >= 0 && focusedIndex < items.length) {
			return focusedIndex;
		}
		return selectedIndex >= 0 ? selectedIndex : 0;
	});

	$effect(() => {
		if (selectedIndex >= 0) {
			focusedIndex = selectedIndex;
		} else if (
			typeof document !== 'undefined' &&
			root &&
			(!document.activeElement || !root.contains(document.activeElement))
		) {
			focusedIndex = null;
		}
	});

	export function focusFirst() {
		focusedIndex = 0;
		if (!root) return;
		const radios = Array.from(root.querySelectorAll<HTMLElement>('[role="radio"]:not(:disabled)'));
		for (let index = 0; index < radios.length; index += 1) {
			radios[index].setAttribute('tabindex', index === 0 ? '0' : '-1');
		}
		radios[0]?.focus();
	}

	function activateIndex(index: number) {
		const entry = items[index];
		if (entry === undefined) return;
		focusedIndex = index;
		if (clearable && selectedIndex === index) {
			onClear?.();
		} else {
			onSelect?.(entry);
		}
	}
</script>

<div
	bind:this={root}
	role="radiogroup"
	aria-labelledby={labelledBy}
	aria-label={labelledBy ? undefined : ariaLabel}
	class={className}
	use:rovingFocus={{
		columns,
		selectedIndex,
		onActiveIndexChange: (index) => {
			focusedIndex = index;
		},
		onMove: selectOnMove
			? (index) => {
					const entry = items[index];
					if (entry !== undefined) onSelect?.(entry);
				}
			: undefined,
		onClear: clearable ? () => onClear?.() : undefined,
	}}
>
	{#each items as entry, index (getValue(entry))}
		{@render renderItem(
			entry,
			{
				checked: selectedIndex === index,
				tabIndex: activeIndex === index ? 0 : -1,
				select: () => activateIndex(index),
			},
			index,
		)}
	{/each}
</div>
