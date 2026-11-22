<script lang="ts">
	import { enhance } from '$app/forms';
	import AccountHeader from '#lib/components/AccountHeader.svelte';
	import ProfileField from '#lib/components/ProfileField.svelte';
	import type { ActionData, PageData } from './$types';

	let { data, form }: { data: PageData; form: ActionData } = $props();

	const hasLocalUsername = $derived(
		data.methods.has_local_username ??
			data.user.has_local_username ??
			!data.user.username.trim().toLowerCase().startsWith('oidc:')
	);
	const activeDisplayName = $derived(
		data.user.display_name.trim() || (hasLocalUsername ? data.user.username : 'SSO user')
	);
	const localLoginReady = $derived(Boolean(data.methods.password_enabled && hasLocalUsername));
	const canUnlinkSso = $derived(localLoginReady || data.methods.oidc_identities.length > 1);
</script>

<svelte:head><title>Account · Bookward</title></svelte:head>

<a href="#main-content" class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:btn focus:preset-filled-primary-500">Skip to content</a>
<AccountHeader user={data.user} />
<main id="main-content" tabindex="-1" class="mx-auto max-w-3xl px-4 py-10 sm:px-6">
	{#if !data.forcePasswordChange}<a href="/" class="text-primary-600-400">← Bookward</a>{/if}
	<h1 class="{data.forcePasswordChange ? '' : 'mt-6 '}text-3xl font-semibold">Account</h1>
	<p class="mt-2 text-surface-700-300">Signed in as <strong>{activeDisplayName}</strong>. Your recommendations and reading lists are private to this profile.</p>
	{#if data.forcePasswordChange}<p class="mt-5 rounded-xl p-4 preset-tonal-warning" role="alert">Set a personal password before continuing. Your administrator supplied a temporary password for this first sign-in.</p>{/if}
	{#if form?.message && form.error}
		<p class="mt-5 rounded-xl p-4 preset-tonal-error" role="alert">{form.message}</p>
	{:else if form?.message}
		<p class="mt-5 rounded-xl p-4 preset-tonal-success" role="status">{form.message}</p>
	{:else if data.reauthenticationRequired}
		<p class="mt-5 rounded-xl p-4 preset-tonal-warning" role="alert">For security, sign out and sign back in before linking single sign-on.</p>
	{:else if data.linkError}
		<p class="mt-5 rounded-xl p-4 preset-tonal-error" role="alert">The identity could not be linked. Check that the provider identity is not already linked to another account, then try again.</p>
	{:else if data.identityLinked}
		<p class="mt-5 rounded-xl p-4 preset-tonal-success" role="status">Single sign-on is linked to this profile.</p>
	{/if}
	<section class="mt-8 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Password</h2>
		<p class="mt-1 text-sm text-surface-700-300">
			{#if localLoginReady}
				Change your local password for <strong>{data.user.username}</strong>.
			{:else if hasLocalUsername}
				Set a local password as another way to sign in.
			{:else}
				Choose a username and password as another way to sign in.
			{/if}
		</p>
		<form method="POST" action="?/changePassword" use:enhance class="mt-5 grid gap-4">
			<ProfileField profileId={data.user?.profile_id} />
			{#if !hasLocalUsername}
				<label class="grid gap-2 text-sm font-medium">
					Username
					<input
						class="input"
						name="username"
						autocomplete="username"
						autocapitalize="none"
						spellcheck="false"
						maxlength="160"
						required
						aria-describedby="account-username-hint"
					/>
					<span id="account-username-hint" class="text-xs font-normal text-surface-700-300">Choose a unique username for local sign-in. Enrolling local credentials requires a sign-in within the last 10 minutes.</span>
				</label>
			{/if}
			{#if localLoginReady}
				<label class="grid gap-2 text-sm font-medium">Current password<input class="input" type="password" name="current_password" autocomplete="current-password" required /></label>
			{/if}
			<label class="grid gap-2 text-sm font-medium">
				New password
				<input
					class="input"
					type="password"
					name="new_password"
					autocomplete="new-password"
					minlength="12"
					maxlength="1024"
					required
					aria-describedby="account-password-hint"
				/>
				<span id="account-password-hint" class="text-xs font-normal text-surface-700-300">Use 12 to 1024 characters.</span>
			</label>
			<button class="btn preset-filled-primary-500 justify-self-start" type="submit">Update password</button>
		</form>
	</section>
	<section class="mt-6 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Single sign-on</h2>
		<p class="mt-1 text-sm text-surface-700-300">Link your provider once. Future SSO sign-ins open this profile automatically.</p>
		{#if data.methods.oidc_identities.length}
			<div class="mt-4 grid gap-3">
				{#each data.methods.oidc_identities as identity}
					<div class="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-surface-100-800 p-3">
						<div><p class="break-all font-medium">{identity.issuer}</p><p class="text-sm text-surface-700-300">{identity.email || 'Connected identity'}</p></div>
						{#if canUnlinkSso}
							<details>
								<summary class="btn btn-sm cursor-pointer preset-tonal-error" aria-label={`Disconnect ${identity.email || identity.issuer} from this account`}>Disconnect</summary>
								<form method="POST" action="?/unlink" use:enhance class="mt-2 grid max-w-xs justify-items-start gap-2">
									<ProfileField profileId={data.user?.profile_id} />
									<input type="hidden" name="id" value={identity.id} />
									<p class="text-xs text-surface-700-300">You will need another sign-in method to access this profile.</p>
									<button class="btn btn-sm preset-filled-error-500" type="submit" aria-label={`Confirm disconnect: ${identity.email || identity.issuer}`}>Confirm disconnect</button>
								</form>
							</details>
						{:else}
							<div class="grid justify-items-end gap-1 text-right">
								<button
									class="btn btn-sm preset-tonal-surface"
									type="button"
									disabled
									aria-label={`Disconnect ${identity.email || identity.issuer} unavailable until another login method is added`}
								>Disconnect</button>
								<p class="max-w-xs text-xs text-surface-700-300">Set a local username and password above before disconnecting your only sign-in method.</p>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
		{#if data.oidcEnabled}
			<a class="btn preset-tonal-primary mt-5" href="/auth/oidc/start?intent=link&amp;profile_id={encodeURIComponent(data.user.profile_id)}">Link SSO identity</a>
			<p class="mt-2 text-xs text-surface-700-300">Linking requires a sign-in within the last 10 minutes. Sign out and back in if asked to confirm.</p>
		{/if}
	</section>
	{#if data.user.role === 'admin'}<a class="btn preset-tonal-surface mt-6" href="/admin">Manage accounts</a>{/if}
</main>
