import { describe, expect, it } from 'vitest';
import { safeNext } from './safe-next';

describe('safeNext', () => {
	it('keeps bounded same-site paths and query strings', () => {
		expect(safeNext('/account?tab=security')).toBe('/account?tab=security');
	});

	it('rejects external, malformed, control-character, and oversized destinations', () => {
		for (const value of [
			'https://evil.example',
			'//evil.example',
			'/\\evil.example',
			'/login\r\nset-cookie',
			`/${'a'.repeat(2048)}`,
		]) expect(safeNext(value)).toBe('/');
	});

	it('falls back for non-string form values', () => {
		expect(safeNext(null)).toBe('/');
		expect(safeNext({ name: 'target.txt' })).toBe('/');
	});
});
