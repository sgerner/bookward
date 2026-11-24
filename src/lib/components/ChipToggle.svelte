<script module lang="ts">
	export function nextChipPressed(pressed: boolean): boolean {
		return !pressed;
	}
</script>

<script lang="ts">
	import type { Snippet } from 'svelte';

	let {
		pressed = $bindable(false),
		label = '',
		ariaLabel,
		activeClass = 'preset-filled-secondary-500',
		inactiveClass = 'preset-tonal-secondary',
		focusClass = 'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500',
		class: className = 'badge badge-sm',
		disabled = false,
		onToggle,
		children,
	}: {
		pressed?: boolean;
		label?: string;
		ariaLabel?: string;
		activeClass?: string;
		inactiveClass?: string;
		focusClass?: string;
		class?: string;
		disabled?: boolean;
		onToggle?: (nextPressed: boolean) => void;
		children?: Snippet;
	} = $props();

	function handleClick() {
		if (disabled) return;
		const next = nextChipPressed(pressed);
		pressed = next;
		onToggle?.(next);
	}
</script>

<button
	type="button"
	{disabled}
	aria-label={ariaLabel}
	aria-pressed={pressed}
	class={`${className} ${pressed ? activeClass : inactiveClass} ${focusClass}`.trim()}
	onclick={handleClick}
>
	{#if children}{@render children()}{:else}{label}{/if}
</button>
