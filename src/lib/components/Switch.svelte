<script lang="ts">
	import { Check } from '@lucide/svelte';

	export type SwitchTone = 'primary' | 'secondary' | 'tertiary';

	type Props = {
		kind?: 'checkbox' | 'submit';
		checked?: boolean;
		tone?: SwitchTone;
		ariaLabel: string;
		id?: string;
		name?: string;
		value?: string;
		disabled?: boolean;
		busy?: boolean;
		onchange?: (event: Event & { currentTarget: HTMLInputElement }) => void;
	};

	let {
		kind = 'checkbox',
		checked = false,
		tone = 'primary',
		ariaLabel,
		id,
		name,
		value,
		disabled = false,
		busy = false,
		onchange,
	}: Props = $props();
</script>

{#snippet trackContent()}
	<span class="switch-thumb" data-checked={checked ? 'true' : 'false'} aria-hidden="true">
		{#if checked}
			<Check size={12} strokeWidth={3} aria-hidden="true" />
		{:else}
			<span class="switch-thumb-idle-dot"></span>
		{/if}
	</span>
{/snippet}

{#if kind === 'submit'}
	<button
		type="submit"
		role="switch"
		class="switch-track switch-submit"
		data-checked={checked ? 'true' : 'false'}
		data-tone={tone}
		aria-label={ariaLabel}
		aria-checked={checked}
		{disabled}
		aria-busy={busy}
	>
		{@render trackContent()}
	</button>
{:else}
	<span class="switch-root">
		<input
			{id}
			class="switch-input"
			type="checkbox"
			{name}
			{value}
			{checked}
			role="switch"
			aria-label={ariaLabel}
			aria-checked={checked}
			{disabled}
			{onchange}
		/>
		<span
			class="switch-track"
			data-checked={checked ? 'true' : 'false'}
			data-tone={tone}
			aria-hidden="true"
		>
			{@render trackContent()}
		</span>
	</span>
{/if}

<style>
	.switch-root {
		position: relative;
		display: inline-flex;
		flex-shrink: 0;
		vertical-align: middle;
	}

	.switch-input {
		position: absolute;
		inset: 0;
		z-index: 1;
		margin: 0;
		width: 100%;
		height: 100%;
		opacity: 0;
		cursor: pointer;
	}

	.switch-input:disabled {
		cursor: not-allowed;
	}

	.switch-track {
		--switch-track-active: var(--switch-track-primary);
		position: relative;
		display: inline-flex;
		align-items: center;
		width: 3rem;
		height: 1.75rem;
		padding: 0.1875rem;
		border-radius: 9999px;
		border: 1.5px solid var(--control-border);
		background-color: var(--switch-track-off);
		transition:
			background-color 160ms ease,
			border-color 160ms ease,
			box-shadow 160ms ease;
	}

	.switch-submit {
		flex-shrink: 0;
		cursor: pointer;
	}

	.switch-track[data-tone='secondary'] {
		--switch-track-active: var(--switch-track-secondary);
	}

	.switch-track[data-tone='tertiary'] {
		--switch-track-active: var(--switch-track-tertiary);
	}

	.switch-track[data-checked='true'] {
		background-color: var(--switch-track-active);
		border-color: var(--switch-track-active);
	}

	.switch-thumb {
		display: grid;
		place-items: center;
		width: 1.25rem;
		height: 1.25rem;
		border-radius: 9999px;
		background-color: var(--switch-thumb-off);
		color: var(--switch-track-off);
		transform: translateX(0) scale(0.88);
		transition:
			transform 160ms ease,
			background-color 160ms ease,
			color 160ms ease;
	}

	.switch-thumb[data-checked='true'] {
		background-color: var(--switch-thumb-on);
		color: var(--switch-track-active);
		transform: translateX(1.25rem) scale(1);
	}

	.switch-thumb-idle-dot {
		width: 0.375rem;
		height: 0.375rem;
		border-radius: 9999px;
		background-color: var(--switch-track-off);
	}

	.switch-submit:not(:disabled):focus-visible,
	.switch-input:not(:disabled):focus-visible + .switch-track {
		outline: 2px solid var(--focus-ring-outer);
		outline-offset: 2px;
		box-shadow:
			0 0 0 2px var(--focus-ring-inner),
			0 0 0 4px var(--focus-ring-outer);
	}

	.switch-input:disabled + .switch-track {
		opacity: 0.5;
	}

	@media (prefers-reduced-motion: reduce) {
		.switch-track,
		.switch-thumb {
			transition: none;
		}
	}

	@media (forced-colors: active) {
		.switch-track {
			border: 2px solid ButtonText;
			background-color: Canvas;
		}

		.switch-track[data-checked='true'] {
			border-color: Highlight;
			background-color: Highlight;
		}

		.switch-thumb {
			background-color: ButtonText;
			color: Canvas;
		}

		.switch-thumb[data-checked='true'] {
			background-color: HighlightText;
			color: Highlight;
		}

		.switch-submit:not(:disabled):focus-visible,
		.switch-input:not(:disabled):focus-visible + .switch-track {
			outline: 2px solid Highlight;
			outline-offset: 2px;
		}
	}
</style>
