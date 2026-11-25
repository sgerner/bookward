<script lang="ts">
	import {
		LIVE_STATUS_DEBOUNCE_MS,
		liveStatus,
	} from '#lib/live-status.js';

	let {
		message = '',
		immediate = false,
		announceOnMount = false,
		debounceMs = LIVE_STATUS_DEBOUNCE_MS,
		contextKey = undefined,
		paused = false,
		class: className = 'sr-only',
	}: {
		message?: string | null;
		immediate?: boolean;
		announceOnMount?: boolean;
		debounceMs?: number;
		contextKey?: string;
		paused?: boolean;
		class?: string;
	} = $props();
</script>

<div
	class={className}
	role="status"
	aria-live="polite"
	aria-atomic="true"
	use:liveStatus={{
		message,
		immediate,
		announceOnMount,
		debounceMs,
		contextKey,
		paused,
	}}
></div>
