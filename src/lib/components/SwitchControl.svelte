<script module lang="ts">
	export type SwitchTone = 'primary' | 'secondary' | 'tertiary';

	export function nextSwitchChecked(checked: boolean): boolean {
		return !checked;
	}
</script>

<script lang="ts">
	import { Check } from '@lucide/svelte';

	let {
		checked = $bindable(false),
		type = 'button',
		tone,
		id,
		name,
		value = 'on',
		ariaLabel,
		ariaLabelledBy,
		ariaDescribedBy,
		disabled = false,
		busy,
		activeClass = 'preset-filled-secondary-500',
		inactiveClass = 'preset-filled-surface-500',
		focusClass = '',
		class: className = '',
		onToggle,
	}: {
		checked?: boolean;
		type?: 'button' | 'submit';
		tone?: SwitchTone;
		id?: string;
		name?: string;
		value?: string;
		ariaLabel?: string;
		ariaLabelledBy?: string;
		ariaDescribedBy?: string;
		disabled?: boolean;
		busy?: boolean;
		activeClass?: string;
		inactiveClass?: string;
		focusClass?: string;
		class?: string;
		onToggle?: (nextChecked: boolean) => void;
	} = $props();

	const resolvedTone = $derived<SwitchTone>(
		tone ??
			(activeClass.includes('tertiary')
				? 'tertiary'
				: activeClass.includes('primary')
					? 'primary'
					: 'secondary'),
	);

	function handleClick() {
		if (disabled || type === 'submit') return;
		const next = nextSwitchChecked(checked);
		checked = next;
		onToggle?.(next);
	}
</script>

{#if type === 'button' && checked && name}
	<input type="hidden" {name} {value} disabled={disabled} />
{/if}
<button
	{id}
	{type}
	role="switch"
	aria-checked={checked}
	aria-label={ariaLabel}
	aria-labelledby={ariaLabelledBy}
	aria-describedby={ariaDescribedBy}
	aria-busy={busy}
	{disabled}
	data-checked={checked ? 'true' : 'false'}
	data-tone={resolvedTone}
	class={`switch-control relative inline-flex h-7 w-12 shrink-0 cursor-pointer items-center rounded-full p-1 transition disabled:cursor-not-allowed disabled:opacity-50 ${checked ? activeClass : inactiveClass} ${focusClass} ${className}`.trim()}
	onclick={handleClick}
>
	<span class="switch-thumb" data-checked={checked ? 'true' : 'false'} aria-hidden="true">
		{#if checked}
			<Check size={12} strokeWidth={3} aria-hidden="true" />
		{:else}
			<span class="switch-thumb-idle-dot"></span>
		{/if}
	</span>
</button>

<style>
	.switch-control.switch-control {
		--switch-track-active: var(--switch-track-secondary);
		padding: 0.1875rem;
		border: 1.5px solid var(--control-border);
		background-color: var(--switch-track-off);
		transition:
			background-color 160ms ease,
			border-color 160ms ease,
			box-shadow 160ms ease;
	}

	.switch-control.switch-control[data-tone='primary'] {
		--switch-track-active: var(--switch-track-primary);
	}

	.switch-control.switch-control[data-tone='tertiary'] {
		--switch-track-active: var(--switch-track-tertiary);
	}

	.switch-control.switch-control[data-checked='true'] {
		background-color: var(--switch-track-active);
		border-color: var(--switch-track-active);
	}

	.switch-thumb {
		display: grid;
		place-items: center;
		width: 1.25rem;
		height: 1.25rem;
		border-radius: 9999px;
		background-color: var(--switch-thumb-off);
		color: var(--switch-track-off);
		transform: translateX(0) scale(0.88);
		transition:
			transform 160ms ease,
			background-color 160ms ease,
			color 160ms ease;
	}

	.switch-thumb[data-checked='true'] {
		background-color: var(--switch-thumb-on);
		color: var(--switch-track-active);
		transform: translateX(1.25rem) scale(1);
	}

	.switch-thumb-idle-dot {
		width: 0.375rem;
		height: 0.375rem;
		border-radius: 9999px;
		background-color: var(--switch-track-off);
	}

	.switch-control.switch-control:not(:disabled):focus-visible {
		outline: 2px solid var(--focus-ring-outer);
		outline-offset: 2px;
		box-shadow:
			0 0 0 2px var(--focus-ring-inner),
			0 0 0 4px var(--focus-ring-outer);
	}

	@media (prefers-reduced-motion: reduce) {
		.switch-control.switch-control,
		.switch-thumb {
			transition: none;
		}
	}

	@media (forced-colors: active) {
		.switch-control.switch-control {
			border: 2px solid ButtonText;
			background-color: Canvas;
		}

		.switch-control.switch-control[data-checked='true'] {
			border-color: Highlight;
			background-color: Highlight;
		}

		.switch-thumb {
			background-color: ButtonText;
			color: Canvas;
		}

		.switch-thumb[data-checked='true'] {
			background-color: HighlightText;
			color: Highlight;
		}

		.switch-control.switch-control:not(:disabled):focus-visible {
			outline: 2px solid Highlight;
			outline-offset: 2px;
		}
	}
</style>
