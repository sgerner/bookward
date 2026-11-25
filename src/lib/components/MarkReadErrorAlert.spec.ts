import { describe, expect, it, vi } from 'vitest';
import { render } from 'svelte/server';
import MarkReadErrorAlert from './MarkReadErrorAlert.svelte';
import { createMarkReadSubmit } from '#lib/reader-form-actions.js';

describe('MarkReadErrorAlert and createMarkReadSubmit', () => {
	it('renders nothing when empty and renders an inline alert inside the dialog when set', () => {
		const empty = render(MarkReadErrorAlert, { props: { message: '' } });
		expect(empty.body).not.toContain('role="alert"');

		const shown = render(MarkReadErrorAlert, {
			props: {
				message: "Bookward's recommendation engine is unavailable.",
				transitionDuration: 0
			}
		});
		expect(shown.body).toContain('role="alert"');
		expect(shown.body).toContain("Bookward's recommendation engine is unavailable.");
	});

	it('rolls back optimistic state and sets inline modal error on failure or error without closing', async () => {
		const errors: string[] = [];
		const pendingCalls: Array<[string, boolean]> = [];
		const rollback = vi.fn();
		const commit = vi.fn();
		const onSuccess = vi.fn();

		const submit = createMarkReadSubmit({
			bookId: 42,
			setError: (msg) => errors.push(msg),
			setPending: (key, val) => pendingCalls.push([key, val]),
			startOptimistic: () => ({ rollback, commit }),
			onSuccess
		});

		const callback = submit({
			formData: new FormData(),
			submitter: null
		} as never);
		expect(errors).toEqual(['']);
		expect(pendingCalls).toEqual([['shortlist-read-42', true]]);

		const update = vi.fn();
		await (callback as (arg: unknown) => Promise<void>)({
			update,
			result: {
				type: 'failure',
				status: 503,
				data: { message: "Bookward's recommendation engine is unavailable.", error: true }
			}
		});

		expect(rollback).toHaveBeenCalledTimes(1);
		expect(commit).not.toHaveBeenCalled();
		expect(onSuccess).not.toHaveBeenCalled();
		expect(update).not.toHaveBeenCalled();
		expect(errors.at(-1)).toBe("Bookward's recommendation engine is unavailable.");
		expect(pendingCalls.at(-1)).toEqual(['shortlist-read-42', false]);
	});

	it('commits optimistic state and clears reading history cache on success', async () => {
		const errors: string[] = [];
		const commit = vi.fn();
		const rollback = vi.fn();
		const onSuccess = vi.fn();
		const update = vi.fn().mockResolvedValue(undefined);

		const submit = createMarkReadSubmit({
			bookId: 42,
			setError: (msg) => errors.push(msg),
			setPending: () => {},
			startOptimistic: () => ({ rollback, commit }),
			onSuccess
		});

		const callback = submit({
			formData: new FormData(),
			submitter: null
		} as never);
		await (callback as (arg: unknown) => Promise<void>)({
			update,
			result: { type: 'success', status: 200, data: { message: 'Marked as read.' } }
		});

		expect(update).toHaveBeenCalledWith({ reset: false, navigate: false });
		expect(onSuccess).toHaveBeenCalledTimes(1);
		expect(commit).toHaveBeenCalledTimes(1);
		expect(rollback).not.toHaveBeenCalled();
		expect(errors).toEqual(['']);
	});
});
