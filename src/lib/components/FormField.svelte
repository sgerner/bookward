<script lang="ts">
	import type { Snippet } from 'svelte';

	export type FormFieldControlAttrs = {
		id: string;
		'aria-describedby'?: string;
		'aria-invalid'?: 'true';
	};

	type Props = {
		id: string;
		label: string;
		srLabel?: string | null;
		hint?: string | null;
		error?: string | null;
		invalid?: boolean;
		describedBy?: string | Array<string | null | undefined | false> | null;
		class?: string;
		labelClass?: string;
		hintClass?: string;
		errorClass?: string;
		children: Snippet<[FormFieldControlAttrs]>;
	};

	let {
		id,
		label,
		srLabel = null,
		hint = null,
		error = null,
		invalid = false,
		describedBy = null,
		class: className = 'grid gap-2',
		labelClass = 'block text-sm font-medium text-surface-800-200',
		hintClass = 'text-xs leading-5 text-surface-700-300',
		errorClass = 'text-xs font-medium leading-5 text-error-600-400',
		children
	}: Props = $props();

	const cleanHint = $derived(typeof hint === 'string' && hint.trim() ? hint.trim() : '');
	const cleanError = $derived(typeof error === 'string' && error.trim() ? error.trim() : '');
	const isInvalid = $derived(Boolean(cleanError || invalid));
	const hintId = $derived(`${id}-hint`);
	const errorId = $derived(`${id}-error`);

	const describedByAttr = $derived.by(() => {
		const extra = Array.isArray(describedBy)
			? describedBy
			: typeof describedBy === 'string'
				? describedBy.split(/\s+/)
				: [];
		const tokens: string[] = [];
		if (cleanError) tokens.push(errorId);
		if (cleanHint && cleanHint.toLowerCase() !== cleanError.toLowerCase()) tokens.push(hintId);
		for (const item of extra) {
			if (typeof item !== 'string') continue;
			for (const token of item.trim().split(/\s+/)) {
				if (token && !tokens.includes(token)) tokens.push(token);
			}
		}
		return tokens.length > 0 ? tokens.join(' ') : undefined;
	});

	const controlAttrs = $derived<FormFieldControlAttrs>({
		id,
		...(describedByAttr ? { 'aria-describedby': describedByAttr } : {}),
		...(isInvalid ? { 'aria-invalid': 'true' as const } : {})
	});
</script>

<div class={className}>
	<label for={id} class={labelClass}
		>{label}{#if srLabel && srLabel.trim()}<span class="sr-only">{` ${srLabel.trim()}`}</span>{/if}</label
	>
	{@render children(controlAttrs)}
	{#if cleanHint}
		<p id={hintId} class={hintClass}>{cleanHint}</p>
	{/if}
	{#if cleanError}
		<p id={errorId} class={errorClass}>{cleanError}</p>
	{/if}
</div>

<style>
	div :global([aria-invalid='true']) {
		--tw-ring-color: var(--color-error-500);
		border-color: var(--color-error-500);
	}
</style>
