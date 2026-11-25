import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	TRANSIENT_ERROR_DISMISS_MS,
	TRANSIENT_NOTICE_DISMISS_MS,
	createNotificationChannels
} from './notification-channels';

describe('split notification channels', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it('shows the Undo banner immediately after an action error and keeps it past 6.5 seconds', () => {
		const channels = createNotificationChannels();

		channels.announceError('The request could not be completed. Your change was reverted.');
		expect(channels.getState().transientNotice).toMatchObject({
			message: 'The request could not be completed. Your change was reverted.',
			error: true
		});
		expect(channels.getState().undoBanner).toBeNull();

		vi.advanceTimersByTime(2000);

		channels.applyForm({
			message: 'Saved to your shortlist.',
			id: 9,
			undo_id: 55,
			undo_label: 'shortlisting this book'
		});

		expect(channels.getState().undoBanner).toEqual({
			key: 'undo-55',
			decisions: [{ id: 9, decision_id: 55 }],
			id: 9,
			undo_id: 55,
			message: 'Saved to your shortlist.',
			undo_label: 'shortlisting this book'
		});

		// Advance well past the 6.5s transient error timer.
		vi.advanceTimersByTime(TRANSIENT_ERROR_DISMISS_MS + 1000);

		expect(channels.getState().transientNotice).toBeNull();
		expect(channels.getState().undoBanner).toEqual({
			key: 'undo-55',
			decisions: [{ id: 9, decision_id: 55 }],
			id: 9,
			undo_id: 55,
			message: 'Saved to your shortlist.',
			undo_label: 'shortlisting this book'
		});

		channels.destroy();
	});

	it('pauses transient notice dismissal while hovered or focused and resumes afterward', () => {
		const channels = createNotificationChannels();

		channels.announceSuccess('Source preference updated.');
		vi.advanceTimersByTime(3000);
		channels.setPointerInside(true);

		vi.advanceTimersByTime(TRANSIENT_NOTICE_DISMISS_MS * 2);
		expect(channels.getState().transientNotice?.message).toBe('Source preference updated.');

		channels.setPointerInside(false);
		vi.advanceTimersByTime(TRANSIENT_NOTICE_DISMISS_MS - 1);
		expect(channels.getState().transientNotice?.message).toBe('Source preference updated.');

		vi.advanceTimersByTime(1);
		expect(channels.getState().transientNotice).toBeNull();

		channels.destroy();
	});

	it('clears the Undo banner when the decision is undone or the book is marked read', () => {
		const channels = createNotificationChannels();

		channels.applyForm({
			message: 'Passed on this recommendation.',
			id: 12,
			undo_id: 88,
			undo_label: 'passing on this book'
		});
		expect(channels.getState().undoBanner?.undo_id).toBe(88);

		channels.clearUndoDecision(88);
		expect(channels.getState().undoBanner).toBeNull();

		channels.applyForm({
			message: 'Saved to your shortlist.',
			id: 14,
			undo_id: 91,
			undo_label: 'shortlisting this book'
		});
		channels.clearUndoForBook(14);
		expect(channels.getState().undoBanner).toBeNull();

		channels.destroy();
	});

	it('keeps hover and focus pause active when a new transient notice replaces an existing one', () => {
		const channels = createNotificationChannels();

		channels.announceSuccess('First notice.');
		channels.setPointerInside(true);
		vi.advanceTimersByTime(2000);

		channels.announceError('Second notice while pointer is still inside.');
		expect(channels.getState().transientNotice).toMatchObject({
			message: 'Second notice while pointer is still inside.',
			error: true
		});

		// Must remain visible indefinitely while pointer stays inside.
		vi.advanceTimersByTime(TRANSIENT_ERROR_DISMISS_MS * 3);
		expect(channels.getState().transientNotice?.message).toBe(
			'Second notice while pointer is still inside.'
		);

		// Once pointer leaves, the full error dismiss window runs before auto-dismiss.
		channels.setPointerInside(false);
		vi.advanceTimersByTime(TRANSIENT_ERROR_DISMISS_MS - 1);
		expect(channels.getState().transientNotice?.message).toBe(
			'Second notice while pointer is still inside.'
		);
		vi.advanceTimersByTime(1);
		expect(channels.getState().transientNotice).toBeNull();

		channels.destroy();
	});

	it('routes error form payloads carrying undo_id or undo_decisions to transientNotice instead of undoBanner', () => {
		const channels = createNotificationChannels();

		channels.applyForm({
			error: true,
			message: 'Could not save decision.',
			id: 19,
			undo_id: 104,
			undo_label: 'shortlisting this book'
		});

		expect(channels.getState().undoBanner).toBeNull();
		expect(channels.getState().transientNotice).toMatchObject({
			message: 'Could not save decision.',
			error: true
		});

		channels.applyForm({
			error: true,
			message: 'Could not shortlist selected books.',
			undo_decisions: [{ id: 7, decision_id: 21 }],
			undo_label: 'shortlisting 1 book'
		});

		expect(channels.getState().undoBanner).toBeNull();
		expect(channels.getState().transientNotice).toMatchObject({
			message: 'Could not shortlist selected books.',
			error: true
		});

		channels.destroy();
	});

	it('tracks batch undo_decisions from bulk shortlist, narrows when one book is marked read, and clears by key', () => {
		const channels = createNotificationChannels();

		channels.applyForm({
			message: '2 books added to your shortlist.',
			undo_decisions: [
				{ id: 7, decision_id: 21 },
				{ id: 8, decision_id: 22 }
			],
			undo_label: 'shortlisting 2 books'
		});

		expect(channels.getState().undoBanner).toEqual({
			key: 'undo-21-22',
			decisions: [
				{ id: 7, decision_id: 21 },
				{ id: 8, decision_id: 22 }
			],
			id: undefined,
			undo_id: undefined,
			message: '2 books added to your shortlist.',
			undo_label: 'shortlisting 2 books'
		});

		channels.clearUndoForBook(7);
		expect(channels.getState().undoBanner).toEqual({
			key: 'undo-22',
			decisions: [{ id: 8, decision_id: 22 }],
			id: 8,
			undo_id: 22,
			message: '2 books added to your shortlist.',
			undo_label: 'shortlisting 2 books'
		});

		channels.clearUndoKey('undo-22');
		expect(channels.getState().undoBanner).toBeNull();

		channels.destroy();
	});
});
