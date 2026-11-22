import { describe, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';

const pageState = vi.hoisted(() => ({
	status: 500,
	error: null as App.Error | null
}));

vi.mock('$app/state', () => ({
	get page() {
		return pageState;
	}
}));

import ErrorPage from './+error.svelte';

describe('+error.svelte', () => {
	it('renders a 403 heading and ORIGIN/proxy administrator hints without echoing untrusted values', () => {
		pageState.status = 403;
		pageState.error = {
			status: 403,
			message: 'http://engine:8000 secret=super-secret x-forwarded-host=evil.example'
		};

		const { body } = render(ErrorPage);

		expect(body).toContain('403');
		expect(body).toContain('Request blocked');
		expect(body).toContain('ORIGIN');
		expect(body).toContain('PROTOCOL_HEADER');
		expect(body).toContain('HOST_HEADER');
		expect(body).toContain('focus-visible:outline-2');
		expect(body).not.toContain('http://engine:8000');
		expect(body).not.toContain('super-secret');
		expect(body).not.toContain('evil.example');
	});

	it('preserves distinct 404 and 503 recovery copy', () => {
		pageState.status = 404;
		pageState.error = { status: 404, message: 'Not Found' };
		const missing = render(ErrorPage);
		expect(missing.body).toContain('Page not found');
		expect(missing.body).toContain('Go to Bookward');

		pageState.status = 503;
		pageState.error = { status: 503, message: 'Service Unavailable' };
		const unavailable = render(ErrorPage);
		expect(unavailable.body).toContain('A short intermission');
		expect(unavailable.body).toContain('recommendation service is running');
	});
});
