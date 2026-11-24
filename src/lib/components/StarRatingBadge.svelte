<script lang="ts">
	let {
		rating,
		class: className = 'badge preset-tonal-secondary',
	}: {
		rating: number | null | undefined;
		class?: string;
	} = $props();
</script>

{#if rating !== null && rating !== undefined && rating > 0}
	<span class={className}>
		<span aria-hidden="true">{rating} ★</span>
		<span class="sr-only">Your rating: {rating} {rating === 1 ? 'star' : 'stars'}</span>
	</span>
{/if}
