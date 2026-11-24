<script lang="ts">
	import { enhance, type SubmitFunction } from '$app/forms';
	import { LogOut, RefreshCw } from '@lucide/svelte';
	import ProfileField from '#lib/components/ProfileField.svelte';

	let {
		profileId,
		pending = false,
		onSubmit
	}: {
		profileId?: string | null;
		pending?: boolean;
		onSubmit: SubmitFunction;
	} = $props();
</script>

<form
	method="POST"
	action="/auth/logout"
	use:enhance={onSubmit}
	class="border-t border-surface-300-700/40 pt-2"
>
	<ProfileField {profileId} />
	<button
		class="flex min-h-11 w-full items-center gap-3 rounded-container px-3 text-left text-sm text-surface-800-200 hover:preset-tonal-surface"
		type="submit"
		disabled={pending}
		aria-busy={pending}
	>
		{#if pending}
			<RefreshCw size={17} class="animate-spin" aria-hidden="true" />
		{:else}
			<LogOut size={17} aria-hidden="true" />
		{/if}Sign out
	</button>
</form>
