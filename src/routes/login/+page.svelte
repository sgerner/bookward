<script lang="ts">
	import { enhance } from '$app/forms';
	import type { ActionData, PageData } from './$types';
	let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<svelte:head>
	<title>Sign in · Bookward</title>
	<meta name="robots" content="noindex, nofollow" />
</svelte:head>

<main class="login-shell">
	<section class="login-card" aria-labelledby="login-title">
		<a class="wordmark" href="/" aria-label="Bookward home">Bookward</a>
		{#if data.setup_required}
			<h1 id="login-title">Set up Bookward</h1>
			<p>Create the first administrator account using the one-time setup token from the engine log.</p>
			<form method="POST" action="?/setup" use:enhance>
				<label>Setup token<input name="setup_token" type="password" autocomplete="one-time-code" required /></label>
				<label>Username<input name="username" autocomplete="username" required maxlength="160" /></label>
				<label>Password<input name="password" type="password" autocomplete="new-password" minlength="12" required /></label>
				{#if form?.message}<p class="error" role="alert">{form.message}</p>{/if}
				<button type="submit">Create administrator</button>
			</form>
		{:else}
			<h1 id="login-title">Sign in</h1>
			<p>Your Bookward profile opens automatically after sign-in.</p>
			{#if data.error}<p class="error" role="alert">{data.error}</p>{/if}
			<form method="POST" action="?/login" use:enhance>
				<input type="hidden" name="next" value={data.next} />
				<label>Username<input name="username" autocomplete="username" required maxlength="160" /></label>
				<label>Password<input name="password" type="password" autocomplete="current-password" required /></label>
				{#if form?.message}<p class="error" role="alert">{form.message}</p>{/if}
				<button type="submit">Sign in</button>
			</form>
			{#if data.oidc_enabled}
				<div class="divider"><span>or</span></div>
				<a class="sso-button" href="/auth/oidc/start?intent=login">Continue with single sign-on</a>
			{/if}
		{/if}
	</section>
</main>

<style>
	.login-shell { min-height: 100dvh; display: grid; place-items: center; padding: 1.5rem; background: var(--color-surface-100, #f5f5f8); }
	.login-card { width: min(100%, 27rem); padding: 2rem; border: 1px solid var(--color-surface-300, #ddd); border-radius: 1.25rem; background: var(--color-surface-50, white); box-shadow: 0 1.5rem 4rem #17152b14; }
	.wordmark { display: inline-block; margin-bottom: 1.5rem; color: var(--color-primary-600, #6457e8); font-weight: 800; font-size: 1.15rem; text-decoration: none; }
	h1 { margin: 0 0 .5rem; font-size: 1.7rem; }
	.login-card > p { margin: 0 0 1.5rem; color: var(--color-surface-700, #585868); line-height: 1.5; }
	form { display: grid; gap: 1rem; }
	label { display: grid; gap: .4rem; font-weight: 600; }
	input { width: 100%; min-height: 2.8rem; padding: .65rem .75rem; border: 1px solid var(--color-surface-400, #c8c8d1); border-radius: .6rem; background: transparent; color: inherit; font: inherit; }
	button, .sso-button { display: inline-flex; min-height: 2.8rem; align-items: center; justify-content: center; border: 0; border-radius: .65rem; background: var(--color-primary-600, #6457e8); color: white; font: inherit; font-weight: 700; text-decoration: none; cursor: pointer; }
	.error { margin: 0; color: #b42318; }
	.divider { display: flex; align-items: center; gap: .75rem; margin: 1.2rem 0; color: var(--color-surface-600, #70707a); }
	.divider::before, .divider::after { content: ''; height: 1px; flex: 1; background: var(--color-surface-300, #ddd); }
	.sso-button { width: 100%; border: 1px solid var(--color-surface-400, #c8c8d1); background: transparent; color: inherit; }
	@media (prefers-color-scheme: dark) { .login-shell { background: #14131b; } .login-card { border-color: #373541; background: #1c1b25; } input { border-color: #494756; } }
</style>
