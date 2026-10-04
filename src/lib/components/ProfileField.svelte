<script lang="ts">
	let { profileId }: { profileId?: string | null } = $props();
</script>

{#if profileId}
	<input type="hidden" name="__profile_id" value={profileId} />
{/if}
