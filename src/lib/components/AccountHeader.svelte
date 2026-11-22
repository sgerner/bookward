<script lang="ts">
	import { LogOut } from '@lucide/svelte';
	import bookwardMark from '#lib/assets/bookward-mark.svg';
	import ProfileField from '#lib/components/ProfileField.svelte';
	import ThemePicker from '#lib/components/ThemePicker.svelte';

	type HeaderUser = {
		profile_id: string;
		username: string;
		has_local_username?: boolean;
		display_name: string;
		role: 'admin' | 'user';
		must_change_password: boolean;
	};

	let { user }: { user: HeaderUser } = $props();

	const hasLocalUsername = $derived(
		user.has_local_username ?? !user.username.trim().toLowerCase().startsWith('oidc:')
	);
	const activeDisplayName = $derived(
		user.display_name.trim() || (hasLocalUsername ? user.username : 'SSO user')
	);
</script>

<header
	class="sticky top-0 z-40 border-b border-primary-500/15 preset-filled-surface-50-950 shadow-lg shadow-primary-500/5 backdrop-blur-xl"
>
	<div
		class="mx-auto flex min-h-16 max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-2 sm:px-6 lg:px-8"
	>
		<a
			href="/"
			class="inline-flex min-h-11 shrink-0 items-center gap-3 text-surface-950-50 no-underline"
			aria-label="Bookward home"
		>
			<span class="grid size-10 shrink-0 overflow-hidden rounded-2xl shadow-lg shadow-primary-500/20">
				<img src={bookwardMark} alt="" class="size-full object-cover" aria-hidden="true" />
			</span>
			<span class="hidden text-lg font-semibold tracking-tight sm:inline">Bookward</span>
		</a>
		<div class="ml-auto flex flex-wrap items-center justify-end gap-2 sm:gap-3">
			<span
				class="max-w-36 truncate text-sm font-medium text-surface-800-200 sm:max-w-56"
				title={activeDisplayName}
			>
				{activeDisplayName}
			</span>
			<ThemePicker />
			<form method="POST" action="/auth/logout">
				<ProfileField profileId={user.profile_id} />
				<button class="btn btn-sm min-h-11 gap-2 preset-tonal-surface" type="submit">
					<LogOut size={16} aria-hidden="true" />
					<span>Sign out</span>
				</button>
			</form>
		</div>
	</div>
</header>
