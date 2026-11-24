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
});
