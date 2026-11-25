import type { SubmitFunction } from '$app/forms';
import { tick } from 'svelte';
import { actionResultMessage } from '#lib/action-result-message.js';

export type OptimisticChange = {
	commit?: () => void;
	rollback?: () => void;
};

export function createMarkReadSubmit(options: {
	bookId: number;
	setError: (message: string) => void;
	setPending: (key: string, pending: boolean) => void;
	startOptimistic: (formData: FormData) => OptimisticChange | void;
	onSuccess: () => void;
}): SubmitFunction {
	const key = `shortlist-read-${options.bookId}`;
	return ({ formData, submitter }) => {
		options.setError('');
		options.setPending(key, true);
		let change: OptimisticChange | void;
		try {
			change = options.startOptimistic(formData);
		} catch {
			change = undefined;
		}
		return async ({ update, result }) => {
			const succeeded = result.type === 'success' || result.type === 'redirect';
			let failed = !succeeded;
			const rollback = () => {
				try {
					change?.rollback?.();
				} catch {
					// A rollback must never obscure the action result.
				}
			};
			try {
				if (!succeeded) {
					rollback();
					options.setError(actionResultMessage(result));
					return;
				}
				await update({ reset: false, navigate: false });
				options.onSuccess();
				change?.commit?.();
			} catch (error) {
				failed = true;
				rollback();
				options.setError(
					error instanceof Error
						? error.message
						: 'The request could not be completed. Your change was reverted.'
				);
			} finally {
				options.setPending(key, false);
				if (failed && typeof HTMLElement !== 'undefined' && submitter instanceof HTMLElement) {
					await tick();
					const dialogNode = submitter.closest('dialog, [role="dialog"]');
					if (
						submitter.isConnected &&
						dialogNode &&
						typeof document !== 'undefined' &&
						!dialogNode.contains(document.activeElement)
					) {
						submitter.focus({ preventScroll: true });
					}
				}
			}
		};
	};
}

export function createLogoutSubmit(options: {
	setPending: (pending: boolean) => void;
	closeMenu: () => void;
	announceError: (message: string) => void;
	notifyLogout?: () => void;
}): SubmitFunction {
	return () => {
		options.setPending(true);
		return async ({ update, result }) => {
			const succeeded = result.type === 'success' || result.type === 'redirect';
			try {
				options.closeMenu();
				if (!succeeded) {
					options.announceError(actionResultMessage(result, 'Sign-out could not be completed.'));
					return;
				}
				if (options.notifyLogout) {
					options.notifyLogout();
				} else if (typeof window !== 'undefined') {
					window.dispatchEvent(new CustomEvent('bookward:logout'));
				}
				await update();
			} catch (error) {
				options.closeMenu();
				options.announceError(
					error instanceof Error ? error.message : 'Sign-out could not be completed.'
				);
			} finally {
				options.setPending(false);
			}
		};
	};
}
