import { describe, expect, it } from 'vitest';
import {
	hasExplicitHttpOrigin,
	oidcCookieDeleteOptions,
	oidcCookieOptions,
	sessionCookieDeleteOptions,
	sessionCookieOptions,
	sessionRefreshCookieOptions
} from './cookies.js';

describe('centralized cookie options', () => {
	it('omits Secure over HTTP across initial session and OIDC set/delete options', () => {
		const httpUrl = new URL('http://192.168.1.50:3000/login');

		expect(sessionCookieOptions(httpUrl, 3600)).toEqual({
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/',
			maxAge: 3600
		});
		expect(sessionCookieDeleteOptions(httpUrl)).toEqual({
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/'
		});
		expect(oidcCookieOptions(httpUrl)).toEqual({
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/auth/oidc',
			maxAge: 600
		});
		expect(oidcCookieDeleteOptions(httpUrl)).toEqual({
			httpOnly: true,
			secure: false,
			sameSite: 'lax',
			path: '/auth/oidc'
		});
	});

	it('sets Secure over HTTPS across session and OIDC set/delete options', () => {
		const httpsUrl = new URL('https://books.example.com/login');

		expect(sessionCookieOptions(httpsUrl)).toMatchObject({ secure: true, path: '/' });
		expect(sessionRefreshCookieOptions(httpsUrl, 86400, { ORIGIN: 'http://127.0.0.1:3000' })).toMatchObject({
			secure: true,
			path: '/'
		});
		expect(sessionCookieDeleteOptions(httpsUrl)).toMatchObject({ secure: true, path: '/' });
		expect(oidcCookieOptions(httpsUrl)).toMatchObject({ secure: true, path: '/auth/oidc' });
		expect(oidcCookieDeleteOptions(httpsUrl)).toMatchObject({ secure: true, path: '/auth/oidc' });
	});

	it('keeps Secure enabled on session refresh unless an explicit plain-HTTP ORIGIN is configured', () => {
		const httpUrl = new URL('http://books.example.com/');

		expect(hasExplicitHttpOrigin({})).toBe(false);
		expect(hasExplicitHttpOrigin({ ORIGIN: '' })).toBe(false);
		expect(hasExplicitHttpOrigin({ ORIGIN: 'not-a-url' })).toBe(false);
		expect(hasExplicitHttpOrigin({ ORIGIN: 'https://books.example.com' })).toBe(false);
		expect(hasExplicitHttpOrigin({ ORIGIN: ' http://127.0.0.1:3000 ' })).toBe(true);

		expect(sessionRefreshCookieOptions(httpUrl, 3600, {})).toMatchObject({
			secure: true,
			maxAge: 3600
		});
		expect(
			sessionRefreshCookieOptions(httpUrl, 3600, { ORIGIN: 'https://books.example.com' })
		).toMatchObject({
			secure: true,
			maxAge: 3600
		});
		expect(
			sessionRefreshCookieOptions(new URL('http://127.0.0.1:3000/'), 3600, {
				ORIGIN: 'http://127.0.0.1:3000'
			})
		).toMatchObject({
			secure: false,
			maxAge: 3600
		});
	});
});
