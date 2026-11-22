export const SESSION_COOKIE = 'bookward_session';
export const SESSION_COOKIE_PATH = '/';
export const OIDC_COOKIE_PATH = '/auth/oidc';
export const DEFAULT_SESSION_MAX_AGE = 24 * 60 * 60;
export const DEFAULT_OIDC_MAX_AGE = 600;

export function isSecureCookieUrl(url: Pick<URL, 'protocol'> | undefined): boolean {
	return url?.protocol === 'https:';
}

export function cookieDeleteOptions(
	url: Pick<URL, 'protocol'> | undefined,
	path: string = SESSION_COOKIE_PATH
) {
	return {
		httpOnly: true,
		secure: isSecureCookieUrl(url),
		sameSite: 'lax' as const,
		path
	};
}

export function sessionCookieOptions(
	url: Pick<URL, 'protocol'> | undefined,
	maxAge: number = DEFAULT_SESSION_MAX_AGE
) {
	return {
		...cookieDeleteOptions(url, SESSION_COOKIE_PATH),
		maxAge
	};
}

export function sessionCookieDeleteOptions(url: Pick<URL, 'protocol'> | undefined) {
	return cookieDeleteOptions(url, SESSION_COOKIE_PATH);
}

export function oidcCookieOptions(
	url: Pick<URL, 'protocol'> | undefined,
	maxAge: number = DEFAULT_OIDC_MAX_AGE
) {
	return {
		...cookieDeleteOptions(url, OIDC_COOKIE_PATH),
		maxAge
	};
}

export function oidcCookieDeleteOptions(url: Pick<URL, 'protocol'> | undefined) {
	return cookieDeleteOptions(url, OIDC_COOKIE_PATH);
}
