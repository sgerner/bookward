<script lang="ts">
	import RadioGroup from '#lib/components/RadioGroup.svelte';
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
		value = null,
		max = 5,
		label = 'Book rating',
		labelledBy,
		clearLabel = 'Clear rating',
		size = 'sm',
		busy = false,
		disabled = false,
		selectOnMove = false,
		groupKey,
		onchange,
	}: Props = $props();

	let radioGroup = $state<{ focusFirst: () => void } | null>(null);
	let lastRadioTrigger: HTMLElement | null = null;

	const uid = $props.id();
	const effectiveLabelledBy = $derived(labelledBy ?? `${uid}-label`);
	const ratings = $derived(Array.from({ length: max }, (_, index) => index + 1));
	const normalizedValue = $derived(
		typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= max
			? value
			: null,
	);

	export function focusFirst() {
		radioGroup?.focusFirst();
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
</script>

<div
	class={`flex flex-wrap items-center ${size === 'sm' ? 'gap-1.5' : 'gap-2'}`}
	aria-busy={busy}
	data-rating-group={groupKey}
>
	{#if !labelledBy}
		<span id={effectiveLabelledBy} class="sr-only">{label}</span>
	{/if}
	<RadioGroup
		bind:this={radioGroup}
		items={ratings}
		value={normalizedValue}
		labelledBy={effectiveLabelledBy}
		columns={1}
		{selectOnMove}
		clearable
		class={`flex flex-wrap items-center ${size === 'sm' ? 'gap-1.5' : 'gap-2'}`}
		onSelect={(rating) => onchange?.(rating, resolveRadioTrigger(), false)}
		onClear={() => {
			if (normalizedValue === null) {
				lastRadioTrigger = null;
				return;
			}
			onchange?.(null, resolveRadioTrigger(), false);
		}}
	>
		{#snippet item(rating, radio)}
			{@const active = (normalizedValue ?? 0) >= rating}
			<button
				type="button"
				role="radio"
				{disabled}
				data-rating-star={groupKey ? `${groupKey}-${rating}` : undefined}
				data-active={active ? 'true' : 'false'}
				aria-label={starButtonLabel(rating)}
				aria-checked={radio.checked}
				tabindex={radio.tabIndex}
				class={`star-rating-radio grid place-items-center border transition duration-150 motion-safe:hover:-translate-y-0.5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 ${
					size === 'sm' ? 'size-9 rounded-lg text-base' : 'size-11 rounded-xl text-xl'
				} ${
					active
						? 'border-amber-600/75 bg-amber-500/15 text-amber-800 shadow-sm dark:border-amber-400/65 dark:bg-amber-400/15 dark:text-amber-300'
						: 'border-surface-400-600/80 bg-surface-50-950/60 text-surface-700-300 hover:border-amber-600/60 hover:text-amber-700 dark:hover:border-amber-400/50 dark:hover:text-amber-300'
				}`}
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
			class={`btn btn-sm ml-1 preset-tonal-surface focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 ${
				size === 'sm' ? 'min-h-9 text-xs' : 'min-h-10'
			}`}
			onclick={(event) => onchange?.(null, event.currentTarget, true)}
		>
			{clearLabel}
		</button>
	{/if}
</div>

<style>
	@media (forced-colors: active) {
		.star-rating-radio {
			border: 1.5px solid ButtonText;
			background-color: Canvas;
			color: ButtonText;
		}

		.star-rating-radio[data-active='true'] {
			border: 2px solid Highlight;
			background-color: Highlight;
			color: HighlightText;
		}

		.star-rating-radio:focus-visible {
			outline: 2px solid Highlight;
			outline-offset: 2px;
		}
	}
</style>
