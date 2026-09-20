<script lang="ts">
	import './layout.css';
	import { onMount } from 'svelte';
	import favicon from '$lib/assets/favicon.svg';
	import { initAppearance } from '$lib/theme';

	let { children } = $props();

	onMount(() => initAppearance());
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
