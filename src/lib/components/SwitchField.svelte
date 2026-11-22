<script lang="ts">
	import type { Snippet } from 'svelte';
	import type { HTMLInputAttributes } from 'svelte/elements';

	type Props = {
		label: string;
		description?: string;
		checked?: boolean;
		id?: string;
		class?: string;
		labelClass?: string;
		descriptionClass?: string;
		activeTrackClass?: string;
		heading?: 'h2' | 'h3';
		icon?: Snippet;
		badge?: Snippet;
	} & Omit<
		HTMLInputAttributes,
		| 'type'
		| 'role'
		| 'checked'
		| 'id'
		| 'class'
		| 'aria-label'
		| 'aria-labelledby'
		| 'aria-describedby'
		| 'children'
	>;

	let {
		label,
		description,
		checked = $bindable(false),
		id,
		class: className = '',
		labelClass = 'font-medium text-surface-950-50',
		descriptionClass = 'mt-0.5 text-xs font-normal text-surface-700-300',
		activeTrackClass = 'preset-filled-secondary-500',
		heading,
		icon,
		badge,
		disabled,
		...rest
	}: Props = $props();

	const fallbackId = $props.id();
	const inputId = $derived(id ?? `${fallbackId}-switch`);
	const descriptionId = $derived(`${inputId}-description`);
	const interactiveCursor = $derived(disabled ? 'cursor-not-allowed' : 'cursor-pointer');
</script>

<div class={`relative flex items-center gap-3 ${className}`.trim()}>
	<input
		{...rest}
		{disabled}
		id={inputId}
		class="sr-only"
		type="checkbox"
		role="switch"
		bind:checked
		aria-describedby={description ? descriptionId : undefined}
	/>
	{#if icon}
		{@render icon()}
	{/if}
	<div class="min-w-0 flex-1">
		{#if badge || heading}
			<div class="flex flex-wrap items-center gap-3">
				{#if heading === 'h2'}
					<h2 class={labelClass}>
						<label
							for={inputId}
							class={`${interactiveCursor} after:absolute after:inset-0 after:z-10 after:content-['']`}
						>{label}</label>
					</h2>
				{:else if heading === 'h3'}
					<h3 class={labelClass}>
						<label
							for={inputId}
							class={`${interactiveCursor} after:absolute after:inset-0 after:z-10 after:content-['']`}
						>{label}</label>
					</h3>
				{:else}
					<label
						for={inputId}
						class={`${interactiveCursor} after:absolute after:inset-0 after:z-10 after:content-[''] ${labelClass}`}
					>{label}</label>
				{/if}
				{#if badge}
					{@render badge()}
				{/if}
			</div>
		{:else}
			<label
				for={inputId}
				class={`block ${interactiveCursor} after:absolute after:inset-0 after:z-10 after:content-[''] ${labelClass}`}
			>{label}</label>
		{/if}
		{#if description}
			<p id={descriptionId} class={descriptionClass}>{description}</p>
		{/if}
	</div>
	<span
		aria-hidden="true"
		class={`relative h-7 w-12 shrink-0 rounded-full p-1 transition ${checked ? activeTrackClass : 'preset-filled-surface-500'}`}
	>
		<span
			class={`block size-5 rounded-full preset-filled-surface-50-950 shadow transition-transform ${checked ? 'translate-x-5' : ''}`}
		></span>
	</span>
</div>
