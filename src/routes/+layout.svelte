<script lang="ts">
	import './layout.css';
	import { onMount } from 'svelte';
	import type { LayoutProps } from './$types';
	import favicon from '#lib/assets/favicon.svg';
	import {
		handleLogoutSubmit,
		handleProfileMessage,
		syncActiveProfile,
	} from '#lib/profile-sync.js';
	import { initAppearance } from '#lib/theme.js';

	let { children, data }: LayoutProps = $props();
	let profileChannel: BroadcastChannel | null = null;
	const browserStorage = () => (typeof localStorage !== 'undefined' ? localStorage : null);

	onMount(() => {
		const cleanupAppearance = initAppearance();
		const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('bookward-profile') : null;
		profileChannel = channel;
		const profileId = () => data.user?.profile_id ?? null;
		const onProfile = (event: MessageEvent<{ type?: string; profile_id?: string }>) => {
			handleProfileMessage(event.data, {
				currentProfileId: profileId(),
				storage: browserStorage(),
				replaceLocation: (url) => window.location.replace(url),
				schedule: (callback, delayMs) => window.setTimeout(callback, delayMs),
			});
		};
		channel?.addEventListener('message', onProfile);
		const addProfileToForm = (event: Event) => {
			if (!profileId() || !(event.target instanceof HTMLFormElement)) return;
			if (event.target.querySelector('input[name="__profile_id"]')) return;
			const field = document.createElement('input');
			field.type = 'hidden'; field.name = '__profile_id'; field.value = profileId()!;
			event.target.append(field);
		};
		const broadcastLogout = (event: Event) => {
			if (event.target instanceof HTMLFormElement) {
				handleLogoutSubmit(
					event.target.getAttribute('action') || event.target.action,
					browserStorage(),
					channel
				);
			}
		};
		document.addEventListener('submit', addProfileToForm, true);
		document.addEventListener('submit', broadcastLogout, true);
		syncActiveProfile(profileId(), {
			storage: browserStorage(),
			channel,
			replaceLocation: (url) => window.location.replace(url),
			checkStoredMismatch: false,
		});
		return () => {
			cleanupAppearance();
			channel?.removeEventListener('message', onProfile);
			channel?.close();
			document.removeEventListener('submit', addProfileToForm, true);
			document.removeEventListener('submit', broadcastLogout, true);
		};
	});
	$effect(() => {
		const storage = browserStorage();
		if (!storage) return;
		syncActiveProfile(data.user?.profile_id, {
			storage,
			channel: profileChannel,
			replaceLocation: (url) => window.location.replace(url),
			checkStoredMismatch: true,
		});
	});
</script>

<svelte:head>
	<link rel="icon" type="image/svg+xml" href={favicon} />
	<link rel="icon" href="/favicon.ico" sizes="any" />
	<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png" />
	<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png" />
	<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png" />
	<link rel="manifest" href="/site.webmanifest" />
	<meta name="application-name" content="Bookward" />
	<meta name="apple-mobile-web-app-title" content="Bookward" />
	<meta name="theme-color" content="#7169f5" />
	<meta name="color-scheme" content="light dark" />
</svelte:head>
{@render children()}
