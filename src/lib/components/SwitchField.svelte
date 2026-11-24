<script lang="ts">
	import type { Snippet } from 'svelte';
	import SwitchControl from './SwitchControl.svelte';

	type Props = {
		label: string;
		description?: string;
		checked?: boolean;
		id?: string;
		name?: string;
		value?: string;
		disabled?: boolean;
		class?: string;
		labelClass?: string;
		descriptionClass?: string;
		activeTrackClass?: string;
		focusTrackClass?: string;
		heading?: 'h2' | 'h3';
		icon?: Snippet;
		badge?: Snippet;
		onToggle?: (nextChecked: boolean) => void;
	};

	let {
		label,
		description,
		checked = $bindable(false),
		id,
		name,
		value = 'on',
		disabled = false,
		class: className = '',
		labelClass = 'font-medium text-surface-950-50',
		descriptionClass = 'mt-0.5 text-xs font-normal text-surface-700-300',
		activeTrackClass = 'preset-filled-secondary-500',
		focusTrackClass,
		heading,
		icon,
		badge,
		onToggle,
	}: Props = $props();

	const fallbackId = $props.id();
	const inputId = $derived(id ?? `${fallbackId}-switch`);
	const labelId = $derived(`${inputId}-label`);
	const descriptionId = $derived(`${inputId}-description`);
	const interactiveCursor = $derived(disabled ? 'cursor-not-allowed' : 'cursor-pointer');
</script>

<div class={`relative flex items-center gap-3 ${className}`.trim()}>
	{#if icon}
		{@render icon()}
	{/if}
	<div class="min-w-0 flex-1">
		{#if badge || heading}
			<div class="flex flex-wrap items-center gap-3">
				{#if heading === 'h2'}
					<h2 class={labelClass}>
						<label
							id={labelId}
							for={inputId}
							class={`${interactiveCursor} after:absolute after:inset-0 after:z-10 after:content-['']`}
						>{label}</label>
					</h2>
				{:else if heading === 'h3'}
					<h3 class={labelClass}>
						<label
							id={labelId}
							for={inputId}
							class={`${interactiveCursor} after:absolute after:inset-0 after:z-10 after:content-['']`}
						>{label}</label>
					</h3>
				{:else}
					<label
						id={labelId}
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
				id={labelId}
				for={inputId}
				class={`block ${interactiveCursor} after:absolute after:inset-0 after:z-10 after:content-[''] ${labelClass}`}
			>{label}</label>
		{/if}
		{#if description}
			<p id={descriptionId} class={descriptionClass}>{description}</p>
		{/if}
	</div>
	<SwitchControl
		id={inputId}
		{name}
		{value}
		{disabled}
		bind:checked
		ariaLabelledBy={labelId}
		ariaDescribedBy={description ? descriptionId : undefined}
		activeClass={activeTrackClass}
		focusClass={focusTrackClass}
		{onToggle}
	/>
</div>
