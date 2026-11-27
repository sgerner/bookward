import type { SubmitFunction } from '$app/forms';
import { tick } from 'svelte';
import { actionResultMessage } from '#lib/action-result-message.js';

export type ActionResultLike = {
	type: string;
	status?: number;
	data?: unknown;
	error?: unknown;
};

export type OptimisticChange = {
	commit?: (result?: ActionResultLike) => void;
	rollback?: () => void;
};

export function createMarkReadSubmit(options: {
	bookId: number;
	setError: (message: string) => void;
	setPending: (key: string, pending: boolean) => void;
	startOptimistic: (formData: FormData) => OptimisticChange | void;
	onSuccess: () => void;
	localOnly?: boolean;
	flushTelemetry?: () => Promise<void>;
}): SubmitFunction {
	const key = `shortlist-read-${options.bookId}`;
	const prepare = (formData: FormData, submitter: HTMLElement | null) => {
		options.setError('');
		options.setPending(key, true);
		let change: OptimisticChange | void;
		try {
			change = options.startOptimistic(formData);
		} catch {
			change = undefined;
		}
		const handleResult: ReturnType<SubmitFunction> = async ({ update, result }) => {
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
				if (options.localOnly) {
					await update({ reset: false, refreshAll: false, navigate: false });
				} else {
					await update({ reset: false, navigate: false });
				}
				options.onSuccess();
				if (options.localOnly) {
					change?.commit?.(result);
				} else {
					change?.commit?.();
				}
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
		return handleResult;
	};
	if (options.localOnly && options.flushTelemetry) {
		const flush = options.flushTelemetry;
		return async ({ formData, submitter }) => {
			const handler = prepare(formData, submitter);
			await flush();
			return handler;
		};
	}
	return ({ formData, submitter }) => prepare(formData, submitter);
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
