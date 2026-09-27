<script lang="ts">
	import './layout.css';
	import { onMount } from 'svelte';
	import type { LayoutProps } from './$types';
	import favicon from '$lib/assets/favicon.svg';
	import { initAppearance } from '$lib/theme';

	let { children, data }: LayoutProps = $props();
	let profileChannel: BroadcastChannel | null = null;
	function storedProfile() { try { return localStorage.getItem('bookward-active-profile'); } catch { return null; } }
	function saveProfile(value: string) { try { localStorage.setItem('bookward-active-profile', value); } catch { /* Profile checks still run through the server session. */ } }

	onMount(() => {
		initAppearance();
		const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('bookward-profile') : null;
		profileChannel = channel;
		const profileId = () => data.user?.profile_id ?? null;
		const onProfile = (event: MessageEvent<{ type?: string; profile_id?: string }>) => {
			if (event.data?.type === 'logout') window.setTimeout(() => window.location.replace('/login'), 800);
			else if (event.data?.type === 'profile' && event.data.profile_id !== profileId()) window.location.replace('/');
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
			if (event.target instanceof HTMLFormElement && event.target.action.includes('/auth/logout')) channel?.postMessage({ type: 'logout' });
		};
		document.addEventListener('submit', addProfileToForm, true);
		document.addEventListener('submit', broadcastLogout, true);
		const current = profileId();
		if (current) {
			saveProfile(current);
			channel?.postMessage({ type: 'profile', profile_id: current });
		}
		return () => {
			channel?.removeEventListener('message', onProfile);
			channel?.close();
			document.removeEventListener('submit', addProfileToForm, true);
			document.removeEventListener('submit', broadcastLogout, true);
		};
	});
	$effect(() => {
		const current = data.user?.profile_id;
		if (!current || typeof localStorage === 'undefined') return;
		const stored = storedProfile();
		if (stored && stored !== current) window.location.replace('/');
		saveProfile(current);
		profileChannel?.postMessage({ type: 'profile', profile_id: current });
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
