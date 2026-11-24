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
</script>

<svelte:head><title>Manage accounts · Bookward</title></svelte:head>

<a href="#main-content" class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:btn focus:preset-filled-primary-500">Skip to content</a>
<main id="main-content" tabindex="-1" class="mx-auto max-w-4xl px-4 py-10 sm:px-6">
	<a href="/" class="text-primary-600-400">← Bookward</a>
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
			{@const hasLocalUsername = account.has_local_username ?? !account.username.trim().toLowerCase().startsWith('oidc:')}
			{@const loginLabel = hasLocalUsername ? account.username : 'SSO-only'}
			{@const headingLabel = account.display_name || loginLabel}
			<article class="rounded-2xl border border-surface-300-700 p-5">
				<div class="flex flex-wrap items-start justify-between gap-4">
					<div><h3 class="font-semibold">{headingLabel}</h3><p class="text-sm text-surface-700-300" aria-label={`${loginLabel}, ${account.role}, ${account.status}`}>{loginLabel} · {account.role} · {account.status}</p></div>
					{#if account.id !== data.current_user_id}<form method="POST" action="?/setStatus" use:enhance>
						<ProfileField profileId={data.user?.profile_id} /><input type="hidden" name="account_id" value={account.id} /><input type="hidden" name="status" value={account.status === 'active' ? 'disabled' : 'active'} /><button class="btn btn-sm {account.status === 'active' ? 'preset-tonal-error' : 'preset-tonal-success'}" type="submit" aria-label={`${account.status === 'active' ? 'Disable' : 'Enable'} account ${headingLabel}`}>{account.status === 'active' ? 'Disable' : 'Enable'}</button></form>{/if}
				</div>
				{#if account.id !== data.current_user_id}
					{#if hasLocalUsername}
						<form method="POST" action="?/resetPassword" use:enhance class="mt-4 flex flex-wrap items-end gap-3">
							<ProfileField profileId={data.user?.profile_id} />
							<input type="hidden" name="account_id" value={account.id} />
							<label class="grid min-w-64 flex-1 gap-2 text-sm font-medium">Set password<input class="input" type="password" name="password" minlength="12" required autocomplete="new-password" aria-label={`Set password for ${headingLabel}`} /></label>
							<button class="btn btn-sm preset-tonal-surface" type="submit" aria-label={`Reset password for ${headingLabel}`}>Reset password</button>
						</form>
					{:else}
						<div class="mt-4 flex flex-wrap items-end gap-3">
							<label class="grid min-w-64 flex-1 gap-2 text-sm font-medium text-surface-700-300">
								Set password
								<input class="input" type="password" disabled aria-label={`Set password unavailable for SSO-only account ${headingLabel}`} aria-describedby={`sso-only-reset-hint-${account.id}`} />
								<span id={`sso-only-reset-hint-${account.id}`} class="text-xs font-normal text-surface-700-300">SSO-only — local password reset is unavailable until this reader sets a username in Account settings.</span>
							</label>
							<button class="btn btn-sm preset-tonal-surface" type="button" disabled aria-label={`Reset password unavailable for ${headingLabel}`}>Reset password</button>
						</div>
					{/if}
				{/if}
			</article>
		{/each}
	</section>
	<section class="mt-8 card preset-tonal-surface p-6">
		<h2 class="text-xl font-semibold">Account registry backups</h2>
		{#if data.identityBackup}
			{@const backup = data.identityBackup}
			<p class="mt-2 text-sm text-surface-700-300">
				{backup.enabled ? `Scheduled every ${backup.interval_hours} hours.` : 'Scheduled backups are off.'}
				{backup.backups.length} snapshot{backup.backups.length === 1 ? '' : 's'} available.
			</p>
			{#if backup.last_success_at}
				<p class="mt-2 text-sm">Last successful backup <time datetime={backup.last_success_at}>{backupTime.format(new Date(backup.last_success_at))} UTC</time></p>
			{/if}
			{#if backup.last_restore_at}
				<p class="mt-2 text-sm">Last restore <time datetime={backup.last_restore_at}>{backupTime.format(new Date(backup.last_restore_at))} UTC</time></p>
			{/if}
			{#if backup.last_error}
				<p class="mt-3 p-3 preset-tonal-error" role="alert">
					{#if backup.last_attempt_at}<time datetime={backup.last_attempt_at}>{backupTime.format(new Date(backup.last_attempt_at))} UTC</time> · {/if}
					{backup.last_error}
				</p>
			{/if}
		{:else}
			<p class="mt-2 text-sm text-surface-700-300">Backup status is unavailable.</p>
		{/if}
	</section>
</main>
