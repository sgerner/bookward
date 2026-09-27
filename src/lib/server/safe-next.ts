/** Keep post-authentication redirects on this application and bounded for cookies. */
export function safeNext(value: unknown): string {
	const next = typeof value === 'string' ? value : '/';
	if (
		next.length > 2048 ||
		!next.startsWith('/') ||
		next.startsWith('//') ||
		next.startsWith('/\\') ||
		/[\u0000-\u001f\u007f]/.test(next)
	) return '/';

	try {
		const base = 'https://bookward.invalid';
		const target = new URL(next, base);
		if (target.origin !== base) return '/';
		return `${target.pathname}${target.search}${target.hash}`;
	} catch {
		return '/';
	}
}
