export const TRANSIENT_NOTICE_DISMISS_MS = 5000;
export const TRANSIENT_ERROR_DISMISS_MS = TRANSIENT_NOTICE_DISMISS_MS + 1500;

export type TransientNotice = {
	id: number;
	message: string;
	error: boolean;
};

export type UndoDecisionItem = {
	id: number;
	decision_id: number;
};

export type UndoBanner = {
	key: string;
	decisions: UndoDecisionItem[];
	id?: number;
	undo_id?: number;
	message: string;
	undo_label?: string;
};

export type NotificationSnapshot = {
	transientNotice: TransientNotice | null;
	undoBanner: UndoBanner | null;
};

export type NotificationFormPayload = {
	message?: string;
	error?: boolean;
	id?: number;
	undo_id?: number;
	undo_decisions?: UndoDecisionItem[];
	undo_label?: string;
} | null | undefined;

export function createNotificationChannels(
	onChange?: (snapshot: NotificationSnapshot) => void,
	options?: {
		dismissMs?: number;
		errorDismissMs?: number;
	}
) {
	const dismissMs = options?.dismissMs ?? TRANSIENT_NOTICE_DISMISS_MS;
	const errorDismissMs = options?.errorDismissMs ?? TRANSIENT_ERROR_DISMISS_MS;

	let transientNotice: TransientNotice | null = null;
	let undoBanner: UndoBanner | null = null;
	let nextId = 0;
	let timer: ReturnType<typeof setTimeout> | null = null;
	let currentDelay = dismissMs;
	let pointerInside = false;
	let focusInside = false;
	let paused = false;
	let lastForm: NotificationFormPayload = undefined;

	function snapshot(): NotificationSnapshot {
		return { transientNotice, undoBanner };
	}

	function emit() {
		onChange?.(snapshot());
	}

	function clearTimer() {
		if (timer !== null) {
			clearTimeout(timer);
			timer = null;
		}
	}

	function resetPauseFlags() {
		pointerInside = false;
		focusInside = false;
		paused = false;
	}

	function scheduleTransientDismiss() {
		clearTimer();
		const activeId = transientNotice?.id;
		if (activeId === undefined || paused || currentDelay <= 0) return;
		timer = setTimeout(() => {
			if (transientNotice?.id !== activeId) return;
			timer = null;
			resetPauseFlags();
			transientNotice = null;
			emit();
		}, currentDelay);
	}

	function syncPause() {
		const shouldPause = pointerInside || focusInside;
		if (shouldPause === paused) return;
		paused = shouldPause;
		if (paused) {
			clearTimer();
		} else if (transientNotice) {
			scheduleTransientDismiss();
		}
	}

	function showTransient(message: string, error: boolean) {
		const trimmed = message.trim();
		if (!trimmed) return;
		clearTimer();
		resetPauseFlags();
		const id = ++nextId;
		transientNotice = { id, message: trimmed, error };
		currentDelay = error ? errorDismissMs : dismissMs;
		scheduleTransientDismiss();
		emit();
	}

	return {
		getState: snapshot,
		applyForm(form: NotificationFormPayload) {
			if (form === lastForm) return;
			lastForm = form;
			if (!form?.message || !form.message.trim()) return;
			const decisions: UndoDecisionItem[] = form.undo_decisions?.length
				? form.undo_decisions
				: form.undo_id && form.id
					? [{ id: form.id, decision_id: form.undo_id }]
					: [];
			if (decisions.length) {
				const single = decisions.length === 1 ? decisions[0] : undefined;
				undoBanner = {
					key: `undo-${decisions.map((item) => item.decision_id).join('-')}`,
					decisions,
					id: single?.id ?? form.id,
					undo_id: single?.decision_id ?? form.undo_id,
					message: form.message.trim(),
					undo_label: form.undo_label
				};
				emit();
				return;
			}
			showTransient(form.message, Boolean(form.error));
		},
		announceError(message: string) {
			showTransient(message, true);
		},
		announceSuccess(message: string) {
			showTransient(message, false);
		},
		dismissTransient() {
			clearTimer();
			resetPauseFlags();
			if (!transientNotice) return;
			transientNotice = null;
			emit();
		},
		dismissUndo() {
			if (!undoBanner) return;
			undoBanner = null;
			emit();
		},
		clearUndoKey(key: string) {
			if (undoBanner?.key !== key) return;
			undoBanner = null;
			emit();
		},
		clearUndoDecision(decisionId: number) {
			if (!undoBanner?.decisions.some((item) => item.decision_id === decisionId)) return;
			undoBanner = null;
			emit();
		},
		clearUndoForBook(bookId: number) {
			if (!undoBanner?.decisions.some((item) => item.id === bookId)) return;
			const remaining = undoBanner.decisions.filter((item) => item.id !== bookId);
			if (!remaining.length) {
				undoBanner = null;
			} else {
				const single = remaining.length === 1 ? remaining[0] : undefined;
				undoBanner = {
					...undoBanner,
					key: `undo-${remaining.map((item) => item.decision_id).join('-')}`,
					decisions: remaining,
					id: single?.id,
					undo_id: single?.decision_id
				};
			}
			emit();
		},
		setPointerInside(inside: boolean) {
			pointerInside = inside;
			syncPause();
		},
		setFocusInside(inside: boolean) {
			focusInside = inside;
			syncPause();
		},
		destroy() {
			clearTimer();
		}
	};
}
