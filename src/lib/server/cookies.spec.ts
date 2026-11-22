import { describe, expect, it } from 'vitest';
import {
	oidcCookieDeleteOptions,
	oidcCookieOptions,
	sessionCookieDeleteOptions,
	sessionCookieOptions
} from './cookies.js';

describe('centralized cookie options', () => {
	it('omits Secure over HTTP across session and OIDC set/delete options', () => {
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
		expect(sessionCookieDeleteOptions(httpsUrl)).toMatchObject({ secure: true, path: '/' });
		expect(oidcCookieOptions(httpsUrl)).toMatchObject({ secure: true, path: '/auth/oidc' });
		expect(oidcCookieDeleteOptions(httpsUrl)).toMatchObject({ secure: true, path: '/auth/oidc' });
	});
});
