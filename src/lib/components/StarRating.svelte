<script lang="ts">
	import RadioGroup from './RadioGroup.svelte';

	type Props = {
		value?: number;
		max?: number;
		label?: string;
		labelledBy?: string;
		clearLabel?: string;
		onchange?: (rating: number) => void;
	};

	let {
		value = $bindable(0),
		max = 5,
		label = 'Optional book rating',
		labelledBy,
		clearLabel = 'Clear rating',
		onchange,
	}: Props = $props();

	const fallbackLabelId = $props.id();
	const resolvedLabelledBy = $derived(labelledBy ?? fallbackLabelId);
	const ratings = $derived(Array.from({ length: max }, (_, index) => index + 1));

	let ratingGroup = $state<{ focusFirst: () => void } | null>(null);

	function selectRating(next: number) {
		value = next;
		onchange?.(next);
	}

	function clearRating() {
		selectRating(0);
		ratingGroup?.focusFirst();
	}
</script>

<div class="mt-3 flex flex-wrap items-center gap-2">
	{#if !labelledBy}
		<span id={fallbackLabelId} class="sr-only">{label}</span>
	{/if}
	<RadioGroup
		bind:this={ratingGroup}
		items={ratings}
		value={value > 0 ? value : null}
		labelledBy={resolvedLabelledBy}
		columns={1}
		selectOnMove
		clearable
		class="flex flex-wrap items-center gap-2"
		onSelect={(rating) => selectRating(rating)}
		onClear={() => selectRating(0)}
	>
		{#snippet item(rating, radio)}
			{@const active = value >= rating}
			<button
				type="button"
				role="radio"
				class="star-rating-btn grid size-11 place-items-center rounded-xl text-xl"
				data-active={active ? 'true' : 'false'}
				aria-label={`${rating} ${rating === 1 ? 'star' : 'stars'}`}
				aria-checked={radio.checked}
				tabindex={radio.tabIndex}
				onclick={radio.select}
			>
				<span aria-hidden="true">{active ? '★' : '☆'}</span>
			</button>
		{/snippet}
	</RadioGroup>
	{#if value > 0}
		<button
			type="button"
			class="star-rating-clear btn btn-sm ml-1 min-h-10 preset-tonal-surface"
			onclick={clearRating}
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

	.star-rating-btn:hover {
		transform: translateY(-2px);
		border-color: var(--star-active-border);
		color: var(--star-active-fg);
	}

	.star-rating-btn[data-active='true'] {
		border: 2px solid var(--star-active-border);
		background-color: var(--star-active-bg);
		color: var(--star-active-fg);
	}

	.star-rating-btn:focus-visible {
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

		.star-rating-btn:hover {
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

		.star-rating-btn:focus-visible {
			outline: 2px solid Highlight;
			outline-offset: 2px;
		}
	}
</style>
