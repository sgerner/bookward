import { describe, expect, it } from 'vitest';
import { actionErrorMessage, payloadErrorMessage } from './client-errors';

describe('client error message helpers', () => {
	it('reads payload.message first and falls back to payload.detail on 401 and 409 responses', () => {
		expect(
			payloadErrorMessage({ message: 'Explicit message', detail: 'Server detail' }, 'Fallback')
		).toBe('Explicit message');
		expect(
			payloadErrorMessage({ detail: 'Authentication required. Sign in again to continue.' }, 'Fallback')
		).toBe('Authentication required. Sign in again to continue.');
		expect(
			payloadErrorMessage(
				{ detail: 'This page belongs to a different profile. Reload before continuing.' },
				'Fallback'
			)
		).toBe('This page belongs to a different profile. Reload before continuing.');
		expect(payloadErrorMessage({}, 'Fallback')).toBe('Fallback');
		expect(payloadErrorMessage(null, 'Fallback')).toBe('Fallback');
	});

	it('extracts action failure and error messages from either message or detail', () => {
		expect(
			actionErrorMessage({
				type: 'failure',
				data: { message: 'Choose a valid rating.' }
			})
		).toBe('Choose a valid rating.');
		expect(
			actionErrorMessage({
				type: 'error',
				error: { detail: 'This page belongs to a different profile. Reload before continuing.' }
			})
		).toBe('This page belongs to a different profile. Reload before continuing.');
		expect(actionErrorMessage({ type: 'error' }, 'Custom fallback')).toBe('Custom fallback');
	});
});
