import { afterEach, describe, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import DiscoverLoadMoreFooter from './DiscoverLoadMoreFooter.svelte';
import { discoverSentinel } from '#lib/actions/discover-sentinel.js';

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('DiscoverLoadMoreFooter and discoverSentinel', () => {
	it('renders Load more normally and an inline alert + Try again button when pagination fails', () => {
		const normal = render(DiscoverLoadMoreFooter, {
			props: {
				observeEnabled: true,
				loading: false,
				error: '',
				onLoadMore: () => {}
			}
		});
		expect(normal.body).not.toContain('role="alert"');
		expect(normal.body).toContain('Load more');

		const failed = render(DiscoverLoadMoreFooter, {
			props: {
				observeEnabled: true,
				loading: false,
				error: "Bookward's recommendation engine is unavailable.",
				onLoadMore: () => {}
			}
		});
		expect(failed.body).toContain('role="alert"');
		expect(failed.body).toContain("Bookward's recommendation engine is unavailable.");
		expect(failed.body).toContain('Try again');
	});

	it('pauses IntersectionObserver pagination while error is non-empty and resumes once cleared', () => {
		const observers: Array<{
			callback: IntersectionObserverCallback;
			observe: ReturnType<typeof vi.fn>;
			disconnect: ReturnType<typeof vi.fn>;
		}> = [];

		vi.stubGlobal(
			'IntersectionObserver',
			class {
				observe = vi.fn();
				disconnect = vi.fn();
				constructor(callback: IntersectionObserverCallback) {
					observers.push({ callback, observe: this.observe, disconnect: this.disconnect });
				}
			}
		);

		const onIntersect = vi.fn();
		const node = {} as HTMLElement;
		const action = discoverSentinel(node, {
			enabled: true,
			loading: false,
			error: '',
			onIntersect
		});

		expect(observers).toHaveLength(1);
		expect(observers[0]?.observe).toHaveBeenCalledWith(node);

		observers[0]?.callback(
			[{ isIntersecting: true } as IntersectionObserverEntry],
			{} as IntersectionObserver
		);
		expect(onIntersect).toHaveBeenCalledTimes(1);

		// When a pagination request fails, error becomes non-empty and observer disconnects.
		action.update({
			enabled: true,
			loading: false,
			error: 'Recommendations could not be loaded.',
			onIntersect
		});
		expect(observers[0]?.disconnect).toHaveBeenCalledTimes(1);

		// Any stale callback while in the error state is ignored.
		observers[0]?.callback(
			[{ isIntersecting: true } as IntersectionObserverEntry],
			{} as IntersectionObserver
		);
		expect(onIntersect).toHaveBeenCalledTimes(1);

		// After retry succeeds (error cleared, loading false), observer reconnects.
		action.update({
			enabled: true,
			loading: false,
			error: '',
			onIntersect
		});
		expect(observers).toHaveLength(2);
		observers[1]?.callback(
			[{ isIntersecting: true } as IntersectionObserverEntry],
			{} as IntersectionObserver
		);
		expect(onIntersect).toHaveBeenCalledTimes(2);

		action.destroy();
		expect(observers[1]?.disconnect).toHaveBeenCalledTimes(1);
	});

	it('re-observes when resetKey advances normally, while staying disconnected if error is set', () => {
		const observers: Array<{
			observe: ReturnType<typeof vi.fn>;
			disconnect: ReturnType<typeof vi.fn>;
		}> = [];

		vi.stubGlobal(
			'IntersectionObserver',
			class {
				observe = vi.fn();
				disconnect = vi.fn();
				constructor() {
					observers.push({ observe: this.observe, disconnect: this.disconnect });
				}
			}
		);

		const onIntersect = vi.fn();
		const node = {} as HTMLElement;
		const action = discoverSentinel(node, {
			enabled: true,
			loading: false,
			error: '',
			resetKey: '8:24',
			onIntersect
		});
		expect(observers).toHaveLength(1);

		action.update({
			enabled: true,
			loading: false,
			error: '',
			resetKey: '16:24',
			onIntersect
		});
		expect(observers[0]?.disconnect).toHaveBeenCalledTimes(1);
		expect(observers).toHaveLength(2);

		action.update({
			enabled: true,
			loading: false,
			error: 'Recommendations could not be loaded.',
			resetKey: '16:32',
			onIntersect
		});
		expect(observers[1]?.disconnect).toHaveBeenCalledTimes(1);
		expect(observers).toHaveLength(2);

		action.destroy();
	});
});
