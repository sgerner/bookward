<script lang="ts" generics="Value extends string = string">
	import type { Snippet } from 'svelte';
	import type { HTMLSelectAttributes } from 'svelte/elements';

	type Props = {
		label: string;
		srLabel?: string | null;
		value?: Value;
		id?: string;
		describedBy?: string | Array<string | null | undefined | false> | null;
		class?: string;
		labelClass?: string;
		selectClass?: string;
		children: Snippet;
	} & Omit<
		HTMLSelectAttributes,
		'value' | 'id' | 'class' | 'aria-label' | 'aria-labelledby' | 'aria-describedby' | 'children'
	>;

	let {
		label,
		srLabel = null,
		value = $bindable(),
		id,
		describedBy = null,
		class: className = '',
		labelClass = 'block',
		selectClass = 'select mt-1 min-h-11 w-full preset-tonal-surface',
		onchange,
		children,
		...rest
	}: Props = $props();

	const fallbackId = $props.id();
	const selectId = $derived(id ?? `${fallbackId}-select`);
	const cleanSrLabel = $derived(
		typeof srLabel === 'string' && srLabel.trim() ? srLabel.trim() : '',
	);
	const describedByAttr = $derived.by(() => {
		const raw = Array.isArray(describedBy)
			? describedBy
			: typeof describedBy === 'string'
				? describedBy.split(/\s+/)
				: [];
		const tokens: string[] = [];
		for (const item of raw) {
			if (typeof item !== 'string') continue;
			for (const token of item.trim().split(/\s+/)) {
				if (token && !tokens.includes(token)) tokens.push(token);
			}
		}
		return tokens.length > 0 ? tokens.join(' ') : undefined;
	});
</script>

<div class={className}>
	<label for={selectId} class={labelClass}
		>{label}{#if cleanSrLabel}<span class="sr-only">{` ${cleanSrLabel}`}</span>{/if}</label
	>
	<select
		{...rest}
		id={selectId}
		bind:value
		{onchange}
		class={selectClass}
		aria-describedby={describedByAttr}
	>
		{@render children()}
	</select>
</div>
