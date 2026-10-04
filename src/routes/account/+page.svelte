<script lang="ts">
	import { enhance } from '$app/forms';
	import ProfileField from '$lib/components/ProfileField.svelte';
	import type { ActionData, PageData } from './$types';
	let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<svelte:head><title>Account · Bookward</title></svelte:head>

<main class="mx-auto max-w-3xl px-4 py-10 sm:px-6">
	<a href="/" class="text-primary-600-400">← Bookward</a>
	<h1 class="mt-6 text-3xl font-semibold">Account</h1>
	<p class="mt-2 text-surface-700-300">Signed in as <strong>{data.user.display_name || data.user.username}</strong>. Your recommendations and reading lists are private to this profile.</p>
	{#if data.forcePasswordChange}<p class="mt-5 rounded-xl p-4 preset-tonal-warning" role="alert">Set a personal password before continuing. Your administrator supplied a temporary password for this first sign-in.</p>{/if}
	{#if data.reauthenticationRequired}<p class="mt-5 rounded-xl p-4 preset-tonal-warning" role="alert">For security, sign out and sign back in before linking single sign-on.</p>{/if}
	{#if data.identityLinked}<p class="mt-5 rounded-xl p-4 preset-tonal-success" role="status">Single sign-on is linked to this profile.</p>{/if}
	{#if data.linkError}<p class="mt-5 rounded-xl p-4 preset-tonal-error" role="alert">The identity could not be linked. Check that the provider identity is not already linked to another account, then try again.</p>{/if}
	{#if form?.message}<p class="mt-5 rounded-xl p-4 preset-tonal-success" role="status">{form.message}</p>{/if}
	<section class="mt-8 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Password</h2>
		<p class="mt-1 text-sm text-surface-700-300">{data.methods.password_enabled ? 'Change your local password.' : 'Set a local password as another way to sign in.'}</p>
		<form method="POST" action="?/changePassword" use:enhance class="mt-5 grid gap-4">
			<ProfileField profileId={data.user?.profile_id} />
			{#if data.methods.password_enabled}<label class="grid gap-2 text-sm font-medium">Current password<input class="input" type="password" name="current_password" autocomplete="current-password" required /></label>{/if}
			<label class="grid gap-2 text-sm font-medium">New password<input class="input" type="password" name="new_password" autocomplete="new-password" minlength="12" required /></label>
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
						<form method="POST" action="?/unlink" use:enhance>
							<ProfileField profileId={data.user?.profile_id} /><input type="hidden" name="id" value={identity.id} /><button class="btn btn-sm preset-tonal-error" type="submit">Disconnect</button></form>
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
