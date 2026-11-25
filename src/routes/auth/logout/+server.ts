import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { SESSION_COOKIE, sessionCookieDeleteOptions } from '#lib/server/cookies.js';
import { engine } from '#lib/server/engine.js';

export const POST: RequestHandler = async ({ cookies, url }) => {
	try {
		await engine('/auth/logout', { method: 'POST', body: '{}' });
	} catch {
		// Best-effort upstream revocation: always end the local browser session below.
	} finally {
		cookies.delete(SESSION_COOKIE, sessionCookieDeleteOptions(url));
	}
	// Keep the local session ended even when automatic OIDC sign-in is enabled.
	throw redirect(303, '/login?manual=1');
};
