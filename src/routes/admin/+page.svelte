<script lang="ts">
	import { enhance } from '$app/forms';
	import type { ActionData, PageData } from './$types';
	let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<svelte:head><title>Manage accounts · Bookward</title></svelte:head>

<main class="mx-auto max-w-4xl px-4 py-10 sm:px-6">
	<a href="/" class="text-primary-600-400">← Bookward</a>
	<h1 class="mt-6 text-3xl font-semibold">Manage accounts</h1>
	<p class="mt-2 text-surface-700-300">Each account opens one private profile. Existing data stays with the original administrator profile.</p>
	{#if form?.message}<p class="mt-5 rounded-xl p-4 preset-tonal-success" role="status">{form.message}</p>{/if}
	{#if form?.error}<p class="mt-5 rounded-xl p-4 preset-tonal-error" role="alert">{form.message}</p>{/if}
	<section class="mt-8 rounded-2xl border border-surface-300-700 p-6">
		<h2 class="text-xl font-semibold">Create an account</h2>
		<p class="mt-1 text-sm text-surface-700-300">Share the temporary password privately. The new reader must set a personal password on first sign-in.</p>
		<form method="POST" action="?/create" use:enhance class="mt-5 grid gap-4 sm:grid-cols-2">
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
					<div><h3 class="font-semibold">{account.display_name || account.username}</h3><p class="text-sm text-surface-700-300">{account.username} · {account.role} · {account.status}</p></div>
					{#if account.id !== data.current_user_id}<form method="POST" action="?/setStatus" use:enhance><input type="hidden" name="account_id" value={account.id} /><input type="hidden" name="status" value={account.status === 'active' ? 'disabled' : 'active'} /><button class="btn btn-sm {account.status === 'active' ? 'preset-tonal-error' : 'preset-tonal-success'}" type="submit">{account.status === 'active' ? 'Disable' : 'Enable'}</button></form>{/if}
				</div>
				{#if account.id !== data.current_user_id}<form method="POST" action="?/resetPassword" use:enhance class="mt-4 flex flex-wrap items-end gap-3">
					<input type="hidden" name="account_id" value={account.id} />
					<label class="grid min-w-64 flex-1 gap-2 text-sm font-medium">Set password<input class="input" type="password" name="password" minlength="12" required autocomplete="new-password" /></label>
					<button class="btn btn-sm preset-tonal-surface" type="submit">Reset password</button>
				</form>{/if}
			</article>
		{/each}
	</section>
</main>
