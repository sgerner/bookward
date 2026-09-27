import { redirect } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { engine } from '$lib/server/engine';

export const POST: RequestHandler = async ({ cookies }) => {
	try {
		await engine('/auth/logout', { method: 'POST', body: '{}' });
	} finally {
		cookies.delete('bookward_session', { path: '/' });
	}
	// Keep the local session ended even when automatic OIDC sign-in is enabled.
	throw redirect(303, '/login?manual=1');
};
