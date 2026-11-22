import { describe, expect, it } from 'vitest';
import { actionResultMessage } from './action-result-message';

describe('actionResultMessage', () => {
	it('prefers explicit message fields on action failure data and errors', () => {
		expect(
			actionResultMessage({
				type: 'failure',
				data: { message: 'Goodreads CSV exports must be 10 MB or smaller.' }
			})
		).toBe('Goodreads CSV exports must be 10 MB or smaller.');

		expect(
			actionResultMessage({
				type: 'error',
				error: { message: 'CSV is larger than 10 MB' }
			})
		).toBe('CSV is larger than 10 MB');
	});

	it('falls back to detail fields on raw hook JSON error payloads', () => {
		expect(
			actionResultMessage({
				type: 'failure',
				data: { detail: 'This page belongs to a different profile. Reload before continuing.' }
			})
		).toBe('This page belongs to a different profile. Reload before continuing.');

		expect(
			actionResultMessage({
				type: 'error',
				error: { detail: 'Request origin could not be verified' }
			})
		).toBe('Request origin could not be verified');
	});

	it('returns the generic recovery message when no non-empty message or detail is present', () => {
		expect(actionResultMessage({ type: 'error', error: { message: '   ', detail: '' } })).toBe(
			'The request could not be completed. Your change was reverted.'
		);
	});
});
