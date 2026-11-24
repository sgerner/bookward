<script module lang="ts">
	import type { ChangeEventHandler } from 'svelte/elements';

	export function applySelectValueChange<Value extends string>(options: {
		event: Parameters<ChangeEventHandler<HTMLSelectElement>>[0];
		setValue: (nextValue: Value) => void;
		onChange?: ChangeEventHandler<HTMLSelectElement> | null;
	}): Value {
		const next = options.event.currentTarget.value as Value;
		options.setValue(next);
		options.onChange?.(options.event);
		return next;
	}
</script>

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
		onchange,
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
		onchange={(event) =>
			applySelectValueChange({
				event,
				setValue: (next) => {
					value = next;
				},
				onChange: onchange,
			})}
		class={selectClass}
		aria-labelledby={context ? `${labelId} ${contextId}` : labelId}
	>
		{@render children()}
	</select>
</div>
