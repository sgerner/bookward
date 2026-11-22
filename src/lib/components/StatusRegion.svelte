<script lang="ts">
	import { untrack } from 'svelte';
	import {
		LIVE_STATUS_DEBOUNCE_MS,
		createLiveStatusController,
	} from '#lib/live-status.js';

	let {
		message = '',
		immediate = false,
		announceOnMount = false,
		debounceMs = LIVE_STATUS_DEBOUNCE_MS,
		class: className = 'sr-only',
	}: {
		message?: string | null;
		immediate?: boolean;
		announceOnMount?: boolean;
		debounceMs?: number;
		class?: string;
	} = $props();

	let announcedMessage = $state('');
	let controller: ReturnType<typeof createLiveStatusController> | null = null;

	$effect(() => {
		const instance = createLiveStatusController({
			debounceMs,
			announceOnMount,
			onAnnounce: (nextMessage) => {
				announcedMessage = nextMessage;
			},
		});
		controller = instance;
		untrack(() => {
			instance.update(message, { immediate });
		});
		return () => {
			instance.destroy();
			if (controller === instance) controller = null;
		};
	});

	$effect(() => {
		const nextMessage = message;
		const nextImmediate = immediate;
		controller?.update(nextMessage, { immediate: nextImmediate });
	});
</script>

<div class={className} role="status" aria-live="polite" aria-atomic="true">{announcedMessage}</div>
