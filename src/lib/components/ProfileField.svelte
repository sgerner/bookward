<script lang="ts">
	let {
		profileId,
		view
	}: {
		profileId?: string | null;
		view?: string | null;
	} = $props();
</script>

{#if profileId}
	<input type="hidden" name="__profile_id" value={profileId} />
{/if}
{#if view}
	<input type="hidden" name="view" value={view} />
{/if}
