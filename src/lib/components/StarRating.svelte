<script lang="ts">
	import RadioGroup from './RadioGroup.svelte';
	import { starButtonLabel } from '#lib/reading-history-ratings.js';

	type Props = {
		value?: number | null;
		max?: number;
		label?: string;
		labelledBy?: string;
		clearLabel?: string;
		size?: 'sm' | 'md';
		busy?: boolean;
		disabled?: boolean;
		selectOnMove?: boolean;
		groupKey?: string;
		onchange?: (
			rating: number | null,
			trigger: HTMLElement | null,
			clearedViaButton: boolean,
		) => void;
	};

	let {
		value = $bindable(0),
		max = 5,
		label = 'Optional book rating',
		labelledBy,
		clearLabel = 'Clear rating',
		size,
		busy,
		disabled = false,
		selectOnMove,
		groupKey,
		onchange,
	}: Props = $props();

	const fallbackLabelId = $props.id();
	const resolvedLabelledBy = $derived(labelledBy ?? fallbackLabelId);
	const resolvedSize = $derived(size ?? (groupKey ? 'sm' : 'md'));
	const resolvedSelectOnMove = $derived(selectOnMove ?? onchange === undefined);
	const ratings = $derived(Array.from({ length: max }, (_, index) => index + 1));
	const normalizedValue = $derived(
		typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= max
			? value
			: null,
	);

	let ratingGroup = $state<{ focusFirst: () => void } | null>(null);
	let lastRadioTrigger: HTMLElement | null = null;
	let clearToNull = $state(value === null);

	$effect(() => {
		if (value === null) {
			clearToNull = true;
		} else if (value === 0) {
			clearToNull = false;
		}
	});

	export function focusFirst() {
		ratingGroup?.focusFirst();
	}

	function resolveRadioTrigger(): HTMLElement | null {
		if (lastRadioTrigger) {
			const trigger = lastRadioTrigger;
			lastRadioTrigger = null;
			return trigger;
		}
		if (typeof document !== 'undefined' && document.activeElement instanceof HTMLElement) {
			return document.activeElement;
		}
		return null;
	}

	function selectRating(next: number, trigger: HTMLElement | null) {
		value = next;
		onchange?.(next, trigger, false);
	}

	function clearFromRadio(trigger: HTMLElement | null) {
		if (normalizedValue === null) {
			lastRadioTrigger = null;
			return;
		}
		value = clearToNull ? null : 0;
		onchange?.(null, trigger, false);
	}

	function clearFromButton(trigger: HTMLElement) {
		if (onchange) {
			onchange(null, trigger, true);
			value = clearToNull ? null : 0;
		} else {
			value = clearToNull ? null : 0;
			ratingGroup?.focusFirst();
		}
	}
</script>

<div
	class={`flex flex-wrap items-center ${resolvedSize === 'sm' ? 'gap-1.5' : 'mt-3 gap-2'}`}
	aria-busy={busy}
	data-rating-group={groupKey}
>
	{#if !labelledBy}
		<span id={fallbackLabelId} class="sr-only">{label}</span>
	{/if}
	<RadioGroup
		bind:this={ratingGroup}
		items={ratings}
		value={normalizedValue}
		labelledBy={resolvedLabelledBy}
		columns={1}
		selectOnMove={resolvedSelectOnMove}
		clearable
		class={`flex flex-wrap items-center ${resolvedSize === 'sm' ? 'gap-1.5' : 'gap-2'}`}
		onSelect={(rating) => selectRating(rating, resolveRadioTrigger())}
		onClear={() => clearFromRadio(resolveRadioTrigger())}
	>
		{#snippet item(rating, radio)}
			{@const active = (normalizedValue ?? 0) >= rating}
			<button
				type="button"
				role="radio"
				{disabled}
				class={`star-rating-btn grid place-items-center disabled:cursor-not-allowed disabled:opacity-60 ${
					resolvedSize === 'sm' ? 'size-9 rounded-lg text-base' : 'size-11 rounded-xl text-xl'
				}`}
				data-rating-star={groupKey ? `${groupKey}-${rating}` : undefined}
				data-active={active ? 'true' : 'false'}
				aria-label={starButtonLabel(rating)}
				aria-checked={radio.checked}
				tabindex={radio.tabIndex}
				onclick={(event) => {
					lastRadioTrigger = event.currentTarget;
					radio.select();
				}}
			>
				<span aria-hidden="true">{active ? '★' : '☆'}</span>
			</button>
		{/snippet}
	</RadioGroup>
	{#if normalizedValue !== null}
		<button
			type="button"
			{disabled}
			data-rating-clear={groupKey}
			class={`star-rating-clear btn btn-sm ml-1 preset-tonal-surface ${
				resolvedSize === 'sm' ? 'min-h-9 text-xs' : 'min-h-10'
			}`}
			onclick={(event) => clearFromButton(event.currentTarget)}
		>
			{clearLabel}
		</button>
	{/if}
</div>

<style>
	.star-rating-btn {
		border: 1.5px solid var(--star-idle-border);
		background-color: var(--star-idle-bg);
		color: var(--star-idle-fg);
		transition:
			transform 160ms ease,
			border-color 160ms ease,
			background-color 160ms ease,
			color 160ms ease,
			box-shadow 160ms ease;
	}

	.star-rating-btn:hover:not(:disabled) {
		transform: translateY(-2px);
		border-color: var(--star-active-border);
		color: var(--star-active-fg);
	}

	.star-rating-btn[data-active='true'] {
		border: 2px solid var(--star-active-border);
		background-color: var(--star-active-bg);
		color: var(--star-active-fg);
	}

	.star-rating-btn:focus-visible,
	.star-rating-clear:focus-visible {
		outline: 2px solid var(--focus-ring-outer);
		outline-offset: 2px;
		box-shadow:
			0 0 0 2px var(--focus-ring-inner),
			0 0 0 4px var(--focus-ring-outer);
	}

	.star-rating-clear {
		border: 1px solid var(--control-border);
	}

	@media (prefers-reduced-motion: reduce) {
		.star-rating-btn {
			transition: none;
		}

		.star-rating-btn:hover:not(:disabled) {
			transform: none;
		}
	}

	@media (forced-colors: active) {
		.star-rating-btn {
			border: 1.5px solid ButtonText;
			background-color: Canvas;
			color: ButtonText;
		}

		.star-rating-btn[data-active='true'] {
			border: 2px solid Highlight;
			background-color: Highlight;
			color: HighlightText;
		}

		.star-rating-btn:focus-visible,
		.star-rating-clear:focus-visible {
			outline: 2px solid Highlight;
			outline-offset: 2px;
		}
	}
</style>
