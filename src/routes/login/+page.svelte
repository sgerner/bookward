<script lang="ts">
	import { enhance } from '$app/forms';
	import type { ActionData, PageData } from './$types';
	import { ArrowRight, BookOpen, Check, KeyRound, Sparkles } from '@lucide/svelte';
	import bookwardMark from '$lib/assets/bookward-mark.svg';

	let { data, form }: { data: PageData; form: ActionData } = $props();
</script>

<svelte:head>
	<title>{data.setup_required ? 'Set up' : 'Sign in'} · Bookward</title>
	<meta name="robots" content="noindex, nofollow" />
</svelte:head>

<main
	class="relative isolate grid min-h-dvh overflow-hidden text-surface-900-100 before:pointer-events-none before:fixed before:inset-0 before:-z-10 before:content-[''] before:bg-[radial-gradient(circle_at_14%_2%,_color-mix(in_oklab,_var(--color-primary-500)_72%,_transparent)_0%,_transparent_36%),radial-gradient(circle_at_86%_8%,_color-mix(in_oklab,_var(--color-secondary-500)_60%,_transparent)_0%,_transparent_32%),radial-gradient(circle_at_52%_100%,_color-mix(in_oklab,_var(--color-tertiary-500)_64%,_transparent)_0%,_transparent_44%)] before:opacity-40 after:pointer-events-none after:fixed after:inset-0 after:-z-10 after:content-[''] after:bg-[linear-gradient(118deg,_transparent_0%,_color-mix(in_oklab,_var(--color-primary-500)_18%,_transparent)_42%,_transparent_68%),linear-gradient(180deg,_transparent_50%,_color-mix(in_oklab,_var(--color-secondary-500)_14%,_transparent)_100%)] after:opacity-70 dark:before:opacity-75 dark:after:opacity-90"
>
	<div
		class="mx-auto grid min-h-dvh w-full max-w-7xl items-center gap-8 px-5 py-8 sm:px-8 sm:py-12 lg:grid-cols-[minmax(0,1fr)_minmax(25rem,32rem)] lg:gap-16 lg:px-12 lg:py-14"
	>
		<section class="hidden min-w-0 lg:block" aria-labelledby="welcome-title">
			<a href="/" class="inline-flex items-center gap-3 text-surface-950-50 no-underline">
				<img src={bookwardMark} alt="" class="size-12 rounded-2xl shadow-lg shadow-primary-500/20" />
				<span>
					<span class="block text-lg font-semibold tracking-tight">Bookward</span>
					<span class="mt-0.5 block text-[0.65rem] font-bold tracking-[0.24em] text-surface-700-300">READ WITH INTENTION</span>
				</span>
			</a>

			<div class="mt-20 max-w-2xl">
				<span class="badge gap-2 preset-tonal-primary"><Sparkles size={14} /> A PERSONAL READING COMPANION</span>
				<h2 id="welcome-title" class="mt-7 max-w-xl text-5xl font-semibold leading-[1.05] tracking-tight text-surface-950-50 xl:text-6xl">
					Find your next favorite book.
				</h2>
				<p class="mt-6 max-w-lg text-lg leading-8 text-surface-700-300">
					A thoughtful space for the books you love, the lists you trust, and the stories waiting for you.
				</p>
			</div>

			<div class="relative mt-14 max-w-xl">
				<div class="absolute -left-6 top-0 size-48 rounded-full bg-primary-500/20 blur-3xl" aria-hidden="true"></div>
				<div class="relative flex items-center gap-5 rounded-3xl border border-primary-500/15 bg-surface-50-950/70 p-5 shadow-xl shadow-primary-500/5 backdrop-blur-xl">
					<div class="relative grid size-20 shrink-0 place-items-center overflow-hidden rounded-[1.65rem] bg-gradient-to-br from-primary-500 via-secondary-500 to-tertiary-500 shadow-lg shadow-primary-500/20">
						<img src={bookwardMark} alt="" class="size-16 rounded-[1.2rem]" aria-hidden="true" />
					</div>
					<div class="min-w-0">
						<p class="text-xs font-bold uppercase tracking-[0.18em] text-surface-600-400">Your own reading world</p>
						<p class="mt-2 text-lg font-semibold leading-snug text-surface-950-50">Personal picks, all in one place.</p>
						<div class="mt-3 flex items-center gap-2 text-sm font-medium text-surface-700-300">
							<Check size={15} class="shrink-0 text-success-500" /> Private to your profile
						</div>
					</div>
				</div>
			</div>
		</section>

		<section class="mx-auto w-full max-w-lg" aria-labelledby="login-title">
			<div class="card relative overflow-hidden rounded-[2rem] border border-primary-500/15 bg-surface-50-950/90 p-6 shadow-2xl shadow-primary-500/10 backdrop-blur-xl sm:p-9">
				<div class="absolute inset-x-0 top-0 h-1 bg-gradient-to-r from-primary-500 via-secondary-500 to-tertiary-500" aria-hidden="true"></div>

				<a href="/" class="mb-8 inline-flex items-center gap-3 text-surface-950-50 no-underline lg:hidden">
					<img src={bookwardMark} alt="" class="size-11 rounded-2xl shadow-lg shadow-primary-500/20" />
					<span>
						<span class="block text-base font-semibold tracking-tight">Bookward</span>
						<span class="mt-0.5 block text-[0.6rem] font-bold tracking-[0.22em] text-surface-700-300">READ WITH INTENTION</span>
					</span>
				</a>

				<div class="mb-6 flex items-center gap-3">
					<span class="grid size-12 shrink-0 place-items-center rounded-2xl preset-tonal-primary">
						{#if data.setup_required}<KeyRound size={21} />{:else}<BookOpen size={21} />{/if}
					</span>
					<span class="text-xs font-bold uppercase tracking-[0.18em] text-surface-600-400">
						{data.setup_required ? 'First-time setup' : 'Welcome back'}
					</span>
				</div>

				{#if data.setup_required}
					<h1 id="login-title" class="text-3xl font-semibold tracking-tight text-surface-950-50 sm:text-4xl">Set up Bookward</h1>
					<p id="setup-description" class="mt-3 text-base leading-7 text-surface-700-300">
						Choose a username and password for the first administrator. Setup closes as soon as this account is created.
					</p>
					<form class="mt-8 grid gap-5" method="POST" action="?/setup" use:enhance aria-describedby="setup-description">
						<label class="grid gap-2 text-sm font-semibold text-surface-800-200">
							Username
							<input class="input min-h-12 w-full" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required maxlength="160" />
						</label>
						<label class="grid gap-2 text-sm font-semibold text-surface-800-200">
							Password
							<input class="input min-h-12 w-full" name="password" type="password" autocomplete="new-password" minlength="12" required aria-describedby="password-help" />
							<span id="password-help" class="text-sm font-normal leading-6 text-surface-700-300">Use at least 12 characters.</span>
						</label>
						{#if form?.message}<p class="rounded-xl border border-error-500/30 p-3 text-sm leading-6 preset-tonal-error" role="alert">{form.message}</p>{/if}
						<button class="btn group min-h-12 w-full justify-between preset-filled-primary-500" type="submit">
							<span>Create administrator</span><ArrowRight size={17} class="transition-transform group-hover:translate-x-0.5" />
						</button>
					</form>
					<p class="mt-6 border-t border-surface-300-700/40 pt-5 text-sm leading-6 text-surface-700-300">
						This first account becomes the administrator. You can add more accounts later from Manage accounts.
					</p>
				{:else}
					<h1 id="login-title" class="text-3xl font-semibold tracking-tight text-surface-950-50 sm:text-4xl">Sign in</h1>
					<p class="mt-3 text-base leading-7 text-surface-700-300">Your Bookward profile opens automatically after you sign in.</p>
					{#if data.error}<p class="mt-5 rounded-xl border border-error-500/30 p-3 text-sm leading-6 preset-tonal-error" role="alert">{data.error}</p>{/if}
					<form class="mt-8 grid gap-5" method="POST" action="?/login" use:enhance>
						<input type="hidden" name="next" value={data.next} />
						<label class="grid gap-2 text-sm font-semibold text-surface-800-200">
							Username
							<input class="input min-h-12 w-full" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required maxlength="160" />
						</label>
						<label class="grid gap-2 text-sm font-semibold text-surface-800-200">
							Password
							<input class="input min-h-12 w-full" name="password" type="password" autocomplete="current-password" required />
						</label>
						{#if form?.message}<p class="rounded-xl border border-error-500/30 p-3 text-sm leading-6 preset-tonal-error" role="alert">{form.message}</p>{/if}
						<button class="btn group min-h-12 w-full justify-between preset-filled-primary-500" type="submit">
							<span>Sign in</span><ArrowRight size={17} class="transition-transform group-hover:translate-x-0.5" />
						</button>
					</form>
					{#if data.oidc_enabled}
						<div class="my-6 flex items-center gap-3 text-xs font-semibold uppercase tracking-[0.16em] text-surface-600-400">
							<span class="h-px flex-1 bg-surface-300-700/50"></span><span>or</span><span class="h-px flex-1 bg-surface-300-700/50"></span>
						</div>
						<a class="btn min-h-12 w-full justify-between preset-tonal-surface" href="/auth/oidc/start?intent=login">
							<span class="inline-flex items-center gap-2"><KeyRound size={16} /> Continue with single sign-on</span><ArrowRight size={16} />
						</a>
					{/if}
				{/if}
			</div>
			<p class="mt-5 text-center text-xs leading-5 text-surface-700-300">Private recommendations. A reading life that stays yours.</p>
		</section>
	</div>
</main>
