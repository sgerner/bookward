export type DiscoverSentinelOptions = {
	enabled: boolean;
	loading: boolean;
	error: string;
	resetKey?: string | number;
	onIntersect: () => void;
};

export function discoverSentinel(node: HTMLElement, initial: DiscoverSentinelOptions) {
	let options = initial;
	let currentResetKey = initial.resetKey;
	let observer: IntersectionObserver | null = null;

	function disconnect() {
		observer?.disconnect();
		observer = null;
	}

	function sync() {
		const shouldObserve =
			options.enabled &&
			!options.loading &&
			!options.error &&
			typeof IntersectionObserver !== 'undefined';
		if (!shouldObserve) {
			disconnect();
			return;
		}
		if (observer) return;
		observer = new IntersectionObserver(
			(entries) => {
				if (
					entries.some((entry) => entry.isIntersecting) &&
					options.enabled &&
					!options.loading &&
					!options.error
				) {
					options.onIntersect();
				}
			},
			{ rootMargin: '240px 0px' }
		);
		observer.observe(node);
	}

	sync();

	return {
		update(next: DiscoverSentinelOptions) {
			const resetChanged = next.resetKey !== currentResetKey;
			options = next;
			currentResetKey = next.resetKey;
			if (resetChanged) {
				disconnect();
			}
			sync();
		},
		destroy() {
			disconnect();
		}
	};
}
