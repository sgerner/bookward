<script lang="ts">
	import { enhance } from '$app/forms';
	import ProfileField from '#lib/components/ProfileField.svelte';
	import type { ActionData, PageData } from './$types';
	let { data, form }: { data: PageData; form: ActionData } = $props();
	const backupTime = new Intl.DateTimeFormat('en', {
		dateStyle: 'medium',
		timeStyle: 'short',
		timeZone: 'UTC'
	});
	let selectedSnapshotOverride = $state('');
	const selectedIdentitySnapshot = $derived(
		data.identityBackup?.backups.some((item) => item.id === selectedSnapshotOverride)
			? selectedSnapshotOverride
			: (data.identityBackup?.backups[0]?.id ?? '<snapshot-id>')
	);
	function formatSnapshotSize(bytes: number) {
		if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
		return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
	}
</script>

<svelte:head><title>Manage accounts · Bookward</title></svelte:head>

<a href="#main-content" class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:btn focus:preset-filled-primary-500">Skip to content</a>
<main id="main-content" tabindex="-1" class="mx-auto max-w-4xl px-4 py-10 sm:px-6">
	<a href="/" class="inline-flex min-h-11 items-center text-primary-600-400">← Bookward</a>
	<h1 class="mt-6 text-3xl font-semibold">Manage accounts</h1>
	<p class="mt-2 text-surface-700-300">Each account opens one private profile. Existing data stays with the original administrator profile.</p>
	{#if form?.message && form.error}<p class="mt-5 rounded-xl p-4 preset-tonal-error" role="alert">{form.message}</p>{:else if form?.message}<p class="mt-5 rounded-xl p-4 preset-tonal-success" role="status">{form.message}</p>{/if}
	<section class="mt-8 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Create an account</h2>
		<p class="mt-1 text-sm text-surface-700-300">Share the temporary password privately. The new reader must set a personal password on first sign-in.</p>
		<form method="POST" action="?/create" use:enhance class="mt-5 grid gap-4 sm:grid-cols-2">
			<ProfileField profileId={data.user?.profile_id} />
			<label class="grid gap-2 text-sm font-medium">Username<input class="input" name="username" autocomplete="off" maxlength="160" required /></label>
			<label class="grid gap-2 text-sm font-medium">Display name<input class="input" name="display_name" maxlength="160" /></label>
			<label class="grid gap-2 text-sm font-medium">Temporary password<input class="input" type="password" name="password" minlength="12" required autocomplete="new-password" /></label>
			<label class="grid gap-2 text-sm font-medium">Role<select class="select" name="role"><option value="user">User</option><option value="admin">Administrator</option></select></label>
			<button class="btn preset-filled-primary-500 justify-self-start" type="submit">Create profile</button>
		</form>
	</section>
	<section class="mt-8 grid gap-4">
		<h2 class="text-xl font-semibold">Accounts</h2>
		{#each data.accounts as account}
			<article class="rounded-2xl border border-surface-300-700 p-5">
				<div class="flex flex-wrap items-start justify-between gap-4">
					<div><h3 class="font-semibold">{account.display_name || account.username}</h3><p class="text-sm text-surface-700-300">{account.username} <span aria-hidden="true">·</span> {account.role} <span aria-hidden="true">·</span> {account.status}</p></div>
					{#if account.id !== data.current_user_id}<form method="POST" action="?/setStatus" use:enhance>
						<ProfileField profileId={data.user?.profile_id} /><input type="hidden" name="account_id" value={account.id} /><input type="hidden" name="status" value={account.status === 'active' ? 'disabled' : 'active'} /><button class="btn btn-sm {account.status === 'active' ? 'preset-tonal-error' : 'preset-tonal-success'}" type="submit" aria-label={`${account.status === 'active' ? 'Disable' : 'Enable'} account ${account.display_name || account.username}`}>{account.status === 'active' ? 'Disable' : 'Enable'}</button></form>{/if}
				</div>
				{#if account.id !== data.current_user_id}<form method="POST" action="?/resetPassword" use:enhance class="mt-4 flex flex-wrap items-end gap-3">
					<ProfileField profileId={data.user?.profile_id} />
					<input type="hidden" name="account_id" value={account.id} />
					<label class="grid min-w-64 flex-1 gap-2 text-sm font-medium">Set password<input class="input" type="password" name="password" minlength="12" required autocomplete="new-password" aria-label={`Set password for ${account.display_name || account.username}`} /></label>
					<button class="btn btn-sm preset-tonal-surface" type="submit" aria-label={`Reset password for ${account.display_name || account.username}`}>Reset password</button>
				</form>{/if}
			</article>
		{/each}
	</section>
	<section class="mt-8 card preset-tonal-surface p-6">
		<div class="flex flex-wrap items-center gap-3">
			<h2 class="text-xl font-semibold">Account registry backups</h2>
			{#if data.identityBackup}
				{#if data.identityBackup.recovery_hold}
					<span class="badge preset-tonal-warning">Recovery hold · backups paused</span>
				{:else}
					<span class="badge {data.identityBackup.enabled ? 'preset-tonal-success' : 'preset-tonal-surface'}">
						{data.identityBackup.enabled ? `Every ${data.identityBackup.interval_hours} hours` : 'Scheduled backups off'}
					</span>
				{/if}
			{/if}
		</div>
		{#if data.identityBackup}
			{@const backup = data.identityBackup}
			<p class="mt-2 text-sm text-surface-700-300">
				{#if backup.recovery_hold}
					Scheduled identity-registry snapshots and retention pruning are paused to protect existing snapshots.
				{:else}
					{backup.enabled ? `Scheduled every ${backup.interval_hours} hours.` : 'Scheduled backups are off.'}
				{/if}
				{backup.backups.length} snapshot{backup.backups.length === 1 ? '' : 's'} available.
			</p>
			{#if backup.recovery_hold}
				<div class="mt-4 space-y-4 border-l-2 border-warning-500 preset-tonal-warning p-4 text-sm leading-6" role="alert">
					<div>
						<p class="font-semibold">Recovery hold active — scheduled identity backups are paused</p>
						<p class="mt-1">
							Bookward initialized a new account registry while {backup.backups.length === 1 ? '1 surviving snapshot was' : `${backup.backups.length} surviving snapshots were`} found in <code>auth-registry</code>. Stop Bookward and run the offline recovery command below to restore prior accounts, or confirm keeping this new registry.
						</p>
					</div>
					<form method="POST" action="?/dismissIdentityHold" use:enhance class="space-y-3 border-t border-warning-500/30 pt-3">
						<ProfileField profileId={data.user?.profile_id} />
						<label class="flex min-h-10 cursor-pointer items-center gap-3 text-xs leading-5">
							<input class="checkbox size-6 shrink-0" type="checkbox" name="confirm" value="KEEP" required />
							<span>Keep this current account registry and resume scheduled identity backups.</span>
						</label>
						<button class="btn btn-sm min-h-10 preset-filled-warning-500" type="submit">Keep current account registry &amp; resume backups</button>
					</form>
				</div>
			{/if}
			{#if backup.last_success_at}
				<p class="mt-3 text-sm">Last successful backup <time datetime={backup.last_success_at}>{backupTime.format(new Date(backup.last_success_at))} UTC</time></p>
			{/if}
			{#if backup.last_restore_at}
				<p class="mt-2 text-sm">Last restore <time datetime={backup.last_restore_at}>{backupTime.format(new Date(backup.last_restore_at))} UTC</time></p>
			{/if}
			{#if backup.backups.length}
				<div class="mt-4 space-y-3 border border-surface-300-700/50 p-4">
					<label class="block text-sm font-medium">
						Snapshot for offline CLI recovery
						<select
							class="select mt-2 w-full"
							name="identityBackupId"
							value={selectedIdentitySnapshot}
							onchange={(event) => (selectedSnapshotOverride = (event.currentTarget as HTMLSelectElement).value)}
						>
							{#each backup.backups as item, index (item.id)}
								<option value={item.id} selected={index === 0}>{backupTime.format(new Date(item.created_at))} UTC · {formatSnapshotSize(item.size_bytes)} · {item.id}</option>
							{/each}
						</select>
					</label>
					<p class="text-xs text-surface-700-300">Stop the engine before restoring the account registry:</p>
					<pre class="overflow-x-auto rounded-lg bg-surface-950/10 p-3 text-xs"><code>docker compose run --rm engine python -m afterword_engine.auth_admin restore {selectedIdentitySnapshot} --confirm</code></pre>
				</div>
			{/if}
			{#if backup.last_error}
				<p class="mt-3 p-3 preset-tonal-error" role="alert">
					{#if backup.last_attempt_at}<time datetime={backup.last_attempt_at}>{backupTime.format(new Date(backup.last_attempt_at))} UTC</time> <span aria-hidden="true">·</span> {/if}
					{backup.last_error}
				</p>
			{/if}
		{:else}
			<p class="mt-2 text-sm text-surface-700-300">Backup status is unavailable.</p>
		{/if}
	</section>
</main>
