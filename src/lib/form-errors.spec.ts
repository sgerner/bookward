import { describe, expect, it } from 'vitest';
import {
	conciseFieldErrorToast,
	fieldError,
	formErrorBanner,
	formSuccessBanner,
	formValue,
	hasFieldErrors,
	isCredentialError
} from './form-errors';

describe('form-errors utilities', () => {
	it('scopes field errors strictly to the matching formId', () => {
		const state = {
			formId: 'resetPassword-acct-1',
			error: true,
			message: 'Use a password between 12 and 1024 characters.',
			fieldErrors: { password: 'Use a password between 12 and 1024 characters.' }
		};

		expect(fieldError(state, 'resetPassword-acct-1', 'password')).toBe(
			'Use a password between 12 and 1024 characters.'
		);
		expect(fieldError(state, 'resetPassword-acct-2', 'password')).toBeUndefined();
		expect(fieldError(state, 'create', 'password')).toBeUndefined();
		expect(hasFieldErrors(state, 'resetPassword-acct-1')).toBe(true);
		expect(hasFieldErrors(state, 'resetPassword-acct-2')).toBe(false);
		expect(formErrorBanner(state, 'resetPassword-acct-1')).toBeUndefined();
	});

	it('exposes non-field error banners and success banners only for the submitted form', () => {
		const failed = {
			formId: 'setStatus-acct-2',
			error: true,
			message: 'The installation must retain an active administrator',
			fieldErrors: {}
		};
		const succeeded = {
			formId: 'changePassword',
			message: 'Password updated.'
		};

		expect(formErrorBanner(failed, 'setStatus-acct-2')).toBe(
			'The installation must retain an active administrator'
		);
		expect(formErrorBanner(failed, 'setStatus-acct-1')).toBeUndefined();
		expect(formSuccessBanner(succeeded, 'changePassword')).toBe('Password updated.');
		expect(formSuccessBanner(succeeded, 'unlink')).toBeUndefined();
	});

	it('handles credential-pair failures, preserved values, and concise toast summaries', () => {
		const single = {
			formId: 'login',
			error: true,
			message: 'Enter your password.',
			fieldErrors: { password: 'Enter your password.' },
			values: { username: 'reader' }
		};
		const credential = {
			formId: 'login',
			error: true,
			credentialError: true,
			message: 'Username or password is incorrect.',
			fieldErrors: {},
			values: { username: 'reader' }
		};

		expect(conciseFieldErrorToast(single)).toBe('Fix the highlighted field and try again.');
		expect(conciseFieldErrorToast(credential)).toBe('Fix the highlighted fields and try again.');
		expect(isCredentialError(credential, 'login')).toBe(true);
		expect(isCredentialError(credential, 'setup')).toBe(false);
		expect(formErrorBanner(credential, 'login')).toBe('Username or password is incorrect.');
		expect(formValue(single, 'login', 'username')).toBe('reader');
		expect(formValue(single, 'setup', 'username')).toBeUndefined();
	});
});
