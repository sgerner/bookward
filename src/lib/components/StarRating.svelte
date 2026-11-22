<script lang="ts">
	type Props = {
		value?: number;
		max?: number;
		label?: string;
		clearLabel?: string;
		onchange?: (rating: number) => void;
	};

	let {
		value = $bindable(0),
		max = 5,
		label = 'Optional book rating',
		clearLabel = 'Clear rating',
		onchange,
	}: Props = $props();

	const ratings = $derived(Array.from({ length: max }, (_, index) => index + 1));

	function selectRating(next: number) {
		value = next;
		onchange?.(next);
	}
</script>

<div class="mt-3 flex flex-wrap items-center gap-2" role="group" aria-label={label}>
	{#each ratings as rating (rating)}
		{@const active = value >= rating}
		<button
			type="button"
			class="star-rating-btn grid size-11 place-items-center rounded-xl text-xl"
			data-active={active ? 'true' : 'false'}
			aria-label={`${rating} ${rating === 1 ? 'star' : 'stars'}`}
			aria-pressed={value === rating}
			onclick={() => selectRating(rating)}
		>
			<span aria-hidden="true">{active ? '★' : '☆'}</span>
		</button>
	{/each}
	{#if value > 0}
		<button
			type="button"
			class="star-rating-clear btn btn-sm ml-1 min-h-10 preset-tonal-surface"
			onclick={() => selectRating(0)}
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
