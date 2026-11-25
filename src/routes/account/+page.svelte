<script lang="ts">
	import { enhance, type SubmitFunction } from '$app/forms';
	import { tick } from 'svelte';
	import FormField from '#lib/components/FormField.svelte';
	import ProfileField from '#lib/components/ProfileField.svelte';
	import {
		fieldError,
		focusFirstInvalidControl,
		formErrorBanner,
		formSuccessBanner
	} from '#lib/form-errors.js';
	import type { ActionData, PageData } from './$types';
	let { data, form }: { data: PageData; form: ActionData } = $props();
	const isFormRateLimited = (formId: string) =>
		Boolean(form && form.formId === formId && 'rateLimited' in form && form.rateLimited);
	const changePasswordRateLimited = $derived(isFormRateLimited('changePassword'));
	const resetHistoryRateLimited = $derived(isFormRateLimited('resetHistory'));
	const deleteAccountRateLimited = $derived(isFormRateLimited('deleteAccount'));

	const enhanceForm: SubmitFunction = ({ formElement }) => {
		return async ({ result, update }) => {
			await update({ reset: result.type === 'success' });
			if (result.type === 'failure') {
				await tick();
				focusFirstInvalidControl(formElement);
			}
		};
	};

	const enhanceDeleteAccount: SubmitFunction = ({ formElement }) => {
		return async ({ result, update }) => {
			if (result.type === 'redirect') {
				try {
					localStorage.removeItem('bookward-active-profile');
				} catch {
					/* Server session is already cleared. */
				}
			}
			await update({ reset: result.type === 'success' });
			if (result.type === 'failure') {
				await tick();
				focusFirstInvalidControl(formElement);
			}
		};
	};
</script>

<svelte:head><title>Account · Bookward</title></svelte:head>

<a href="#main-content" class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:btn focus:preset-filled-primary-500">Skip to content</a>
<main id="main-content" tabindex="-1" class="mx-auto max-w-3xl px-4 py-10 sm:px-6">
	<a href="/" class="inline-flex min-h-11 items-center text-primary-600-400">← Bookward</a>
	<h1 class="mt-6 text-3xl font-semibold">Account</h1>
	<p class="mt-2 text-surface-700-300">Signed in as <strong>{data.user.display_name || data.user.username}</strong>. Your recommendations and reading lists are private to this profile.</p>
	{#if data.forcePasswordChange}<p class="mt-5 rounded-xl p-4 preset-tonal-warning" role="alert">Set a personal password before continuing. Your administrator supplied a temporary password for this first sign-in.</p>{/if}
	{#if data.reauthenticationRequired}
		<p class="mt-5 rounded-xl p-4 preset-tonal-warning" role="alert">For security, sign out and sign back in before linking single sign-on.</p>
	{:else if data.linkError}
		<p class="mt-5 rounded-xl p-4 preset-tonal-error" role="alert">The identity could not be linked. Check that the provider identity is not already linked to another account, then try again.</p>
	{:else if data.identityLinked}
		<p class="mt-5 rounded-xl p-4 preset-tonal-success" role="status">Single sign-on is linked to this profile.</p>
	{/if}
	<section class="mt-8 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Password</h2>
		<p class="mt-1 text-sm text-surface-700-300">{data.methods.password_enabled ? 'Change your local password.' : 'Set a local password as another way to sign in.'}</p>
		{#if formSuccessBanner(form, 'changePassword')}
			<p class="mt-4 rounded-xl p-3 text-sm preset-tonal-success" role="status">{formSuccessBanner(form, 'changePassword')}</p>
		{/if}
		<form method="POST" action="?/changePassword" use:enhance={enhanceForm} class="mt-5 grid gap-4">
			<ProfileField profileId={data.user?.profile_id} />
			{#if data.methods.password_enabled}
				<FormField
					id="account-current-password"
					label="Current password"
					error={fieldError(form, 'changePassword', 'current_password')}
				>
					{#snippet children(field)}
						<input {...field} class="input" type="password" name="current_password" autocomplete="current-password" required />
					{/snippet}
				</FormField>
			{/if}
			<FormField
				id="account-new-password"
				label="New password"
				hint="Use at least 12 characters."
				error={fieldError(form, 'changePassword', 'new_password')}
			>
				{#snippet children(field)}
					<input {...field} class="input" type="password" name="new_password" autocomplete="new-password" minlength="12" required />
				{/snippet}
			</FormField>
			{#if formErrorBanner(form, 'changePassword')}
				<p
					class="rounded-xl p-3 text-sm {changePasswordRateLimited
						? 'preset-tonal-warning'
						: 'preset-tonal-error'}"
					role="alert"
				>
					{formErrorBanner(form, 'changePassword')}
				</p>
			{/if}
			<button class="btn btn-reflow preset-filled-primary-500 justify-self-start" type="submit">Update password</button>
		</form>
	</section>
	<section class="mt-6 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Single sign-on</h2>
		<p class="mt-1 text-sm text-surface-700-300">Link your provider once. Future SSO sign-ins open this profile automatically.</p>
		{#if formSuccessBanner(form, 'unlink')}
			<p class="mt-4 rounded-xl p-3 text-sm preset-tonal-success" role="status">{formSuccessBanner(form, 'unlink')}</p>
		{/if}
		{#if formErrorBanner(form, 'unlink')}
			<p class="mt-4 rounded-xl p-3 text-sm preset-tonal-error" role="alert">{formErrorBanner(form, 'unlink')}</p>
		{/if}
		{#if data.methods.oidc_identities.length}
			<div class="mt-4 grid gap-3">
				{#each data.methods.oidc_identities as identity}
					<div class="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-surface-100-800 p-3">
						<div><p class="break-all font-medium">{identity.issuer}</p><p class="text-sm text-surface-700-300">{identity.email || 'Connected identity'}</p></div>
						<details>
							<summary class="btn btn-sm cursor-pointer preset-tonal-error" aria-label={`Disconnect ${identity.email || identity.issuer} from this account`}>Disconnect</summary>
							<form method="POST" action="?/unlink" use:enhance={enhanceForm} class="mt-2 grid max-w-xs justify-items-start gap-2">
								<ProfileField profileId={data.user?.profile_id} />
								<input type="hidden" name="id" value={identity.id} />
								<p class="text-xs text-surface-700-300">You will need another sign-in method to access this profile.</p>
								<button class="btn btn-sm btn-reflow preset-filled-error-500" type="submit" aria-label={`Confirm disconnect: ${identity.email || identity.issuer}`}>Confirm disconnect</button>
							</form>
						</details>
					</div>
				{/each}
			</div>
		{/if}
		{#if data.oidcEnabled}
			<a class="btn btn-reflow preset-tonal-primary mt-5" href="/auth/oidc/start?intent=link&amp;profile_id={encodeURIComponent(data.user.profile_id)}">Link SSO identity</a>
			<p class="mt-2 text-xs text-surface-700-300">Linking requires a sign-in within the last 10 minutes. Sign out and back in if asked to confirm.</p>
		{/if}
	</section>
	<section class="mt-6 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Reset reading history</h2>
		<p class="mt-1 text-sm text-surface-700-300">Clear your read books, imported shelf history, derived candidate scores, and recommendation decision evidence while keeping your login account, configured sources, and profile backup snapshots.</p>
		{#if formSuccessBanner(form, 'resetHistory')}
			<p class="mt-4 rounded-xl p-3 text-sm preset-tonal-success" role="status">{formSuccessBanner(form, 'resetHistory')}</p>
		{/if}
		<form method="POST" action="?/resetHistory" use:enhance={enhanceForm} class="mt-5 grid gap-4">
			<ProfileField profileId={data.user?.profile_id} />
			{#if data.methods.password_enabled}
				<FormField
					id="reset-history-current-password"
					label="Current password"
					error={fieldError(form, 'resetHistory', 'current_password')}
				>
					{#snippet children(field)}
						<input {...field} class="input" type="password" name="current_password" autocomplete="current-password" required />
					{/snippet}
				</FormField>
			{:else}
				<p class="text-xs text-surface-700-300">Resetting history requires a sign-in within the last 10 minutes. Sign out and back in if asked to confirm.</p>
			{/if}
			<FormField
				id="reset-history-confirm"
				label="Confirmation"
				hint="Type RESET in uppercase to confirm."
				error={fieldError(form, 'resetHistory', 'confirm')}
			>
				{#snippet children(field)}
					<input {...field} class="input" name="confirm" autocomplete="off" pattern="RESET" required />
				{/snippet}
			</FormField>
			{#if formErrorBanner(form, 'resetHistory')}
				<p
					class="rounded-xl p-3 text-sm {resetHistoryRateLimited
						? 'preset-tonal-warning'
						: 'preset-tonal-error'}"
					role="alert"
				>
					{formErrorBanner(form, 'resetHistory')}
				</p>
			{/if}
			<button class="btn btn-reflow preset-tonal-error justify-self-start" type="submit">Reset reading history</button>
		</form>
	</section>
	<section class="mt-6 rounded-2xl border border-error-500/40 p-6">
		<h2 class="text-xl font-semibold">Delete account</h2>
		<p class="mt-1 text-sm text-surface-700-300">Permanently erase this account, connected SSO identities, active sessions, API tokens, personal profile database, encryption key, and profile backup snapshots. This cannot be undone.</p>
		<form
			method="POST"
			action="?/deleteAccount"
			use:enhance={enhanceDeleteAccount}
			class="mt-5 grid gap-4"
		>
			<ProfileField profileId={data.user?.profile_id} />
			{#if data.methods.password_enabled}
				<FormField
					id="delete-account-current-password"
					label="Current password"
					error={fieldError(form, 'deleteAccount', 'current_password')}
				>
					{#snippet children(field)}
						<input {...field} class="input" type="password" name="current_password" autocomplete="current-password" required />
					{/snippet}
				</FormField>
			{:else}
				<p class="text-xs text-surface-700-300">Deleting an SSO-only account requires a sign-in within the last 10 minutes. Sign out and back in if asked to confirm.</p>
			{/if}
			<FormField
				id="delete-account-confirm"
				label="Confirmation"
				hint="Type DELETE in uppercase to confirm."
				error={fieldError(form, 'deleteAccount', 'confirm')}
			>
				{#snippet children(field)}
					<input {...field} class="input" name="confirm" autocomplete="off" pattern="DELETE" required />
				{/snippet}
			</FormField>
			{#if formErrorBanner(form, 'deleteAccount')}
				<p
					class="rounded-xl p-3 text-sm {deleteAccountRateLimited
						? 'preset-tonal-warning'
						: 'preset-tonal-error'}"
					role="alert"
				>
					{formErrorBanner(form, 'deleteAccount')}
				</p>
			{/if}
			<button class="btn btn-reflow preset-filled-error-500 justify-self-start" type="submit">Delete account</button>
		</form>
	</section>
	{#if data.user.role === 'admin'}<a class="btn btn-reflow preset-tonal-surface mt-6" href="/admin">Manage accounts</a>{/if}
</main>
