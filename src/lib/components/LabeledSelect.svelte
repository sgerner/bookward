<script lang="ts" generics="Value extends string = string">
	import type { Snippet } from 'svelte';
	import type { HTMLSelectAttributes } from 'svelte/elements';

	type Props = {
		label: string;
		context?: string;
		value?: Value;
		id?: string;
		class?: string;
		labelClass?: string;
		selectClass?: string;
		children: Snippet;
	} & Omit<
		HTMLSelectAttributes,
		'value' | 'id' | 'class' | 'aria-label' | 'aria-labelledby' | 'children'
	>;

	let {
		label,
		context,
		value = $bindable(),
		id,
		class: className = '',
		labelClass = 'block',
		selectClass = 'select mt-1 min-h-11 w-full preset-tonal-surface',
		children,
		...rest
	}: Props = $props();

	const fallbackId = $props.id();
	const selectId = $derived(id ?? `${fallbackId}-select`);
	const labelId = $derived(`${selectId}-label`);
	const contextId = $derived(`${selectId}-context`);
</script>

<div class={className}>
	<label id={labelId} for={selectId} class={labelClass}>{label}</label>
	{#if context}
		<span id={contextId} class="sr-only">{context}</span>
	{/if}
	<select
		{...rest}
		id={selectId}
		bind:value
		class={selectClass}
		aria-labelledby={context ? `${labelId} ${contextId}` : labelId}
	>
		{@render children()}
	</select>
</div>
