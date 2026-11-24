<script module lang="ts">
	export function nextSwitchChecked(checked: boolean): boolean {
		return !checked;
	}
</script>

<script lang="ts">
	let {
		checked = $bindable(false),
		id,
		name,
		value = 'on',
		ariaLabel,
		ariaLabelledBy,
		disabled = false,
		activeClass = 'preset-filled-secondary-500',
		inactiveClass = 'preset-filled-surface-500',
		focusClass = 'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-secondary-500',
		class: className = '',
		onToggle,
	}: {
		checked?: boolean;
		id?: string;
		name?: string;
		value?: string;
		ariaLabel?: string;
		ariaLabelledBy?: string;
		disabled?: boolean;
		activeClass?: string;
		inactiveClass?: string;
		focusClass?: string;
		class?: string;
		onToggle?: (nextChecked: boolean) => void;
	} = $props();

	function handleClick() {
		if (disabled) return;
		const next = nextSwitchChecked(checked);
		checked = next;
		onToggle?.(next);
	}
</script>

{#if checked && name}
	<input type="hidden" {name} {value} disabled={disabled} />
{/if}
<button
	{id}
	type="button"
	role="switch"
	aria-checked={checked}
	aria-label={ariaLabel}
	aria-labelledby={ariaLabelledBy}
	{disabled}
	class={`relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full p-1 transition disabled:cursor-not-allowed disabled:opacity-50 ${checked ? activeClass : inactiveClass} ${focusClass} ${className}`.trim()}
	onclick={handleClick}
>
	<span
		aria-hidden="true"
		class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition-transform ${checked ? 'translate-x-5' : ''}`}
	></span>
</button>
