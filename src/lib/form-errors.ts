export type FieldErrorMap = Record<string, string>;

export type StructuredFormState = {
	formId?: string;
	error?: boolean;
	credentialError?: boolean;
	message?: string;
	fieldErrors?: FieldErrorMap;
	values?: Record<string, string>;
} | null | undefined;

export function fieldError(
	form: StructuredFormState,
	formId: string,
	field: string
): string | undefined {
	if (!form || form.formId !== formId) return undefined;
	const value = form.fieldErrors?.[field];
	return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

export function fieldErrorCount(form: StructuredFormState, formId?: string): number {
	if (!form) return 0;
	if (formId !== undefined && form.formId !== formId) return 0;
	if (!form.fieldErrors) return 0;
	return Object.values(form.fieldErrors).filter(
		(value) => typeof value === 'string' && value.trim().length > 0
	).length;
}

export function hasFieldErrors(form: StructuredFormState, formId?: string): boolean {
	return fieldErrorCount(form, formId) > 0;
}

export function isCredentialError(form: StructuredFormState, formId?: string): boolean {
	if (!form) return false;
	if (formId !== undefined && form.formId !== formId) return false;
	return Boolean(form.credentialError);
}

export function formErrorBanner(form: StructuredFormState, formId: string): string | undefined {
	if (!form || form.formId !== formId) return undefined;
	if (!form.error) return undefined;
	if (hasFieldErrors(form, formId)) return undefined;
	return typeof form.message === 'string' && form.message.trim() ? form.message.trim() : undefined;
}

export function formSuccessBanner(form: StructuredFormState, formId: string): string | undefined {
	if (!form || form.formId !== formId || form.error) return undefined;
	return typeof form.message === 'string' && form.message.trim() ? form.message.trim() : undefined;
}

export function formValue(
	form: StructuredFormState,
	formId: string,
	field: string
): string | undefined {
	if (!form || form.formId !== formId) return undefined;
	const value = form.values?.[field];
	return typeof value === 'string' ? value : undefined;
}

export function conciseFieldErrorToast(form: StructuredFormState): string {
	if (isCredentialError(form)) {
		return 'Fix the highlighted fields and try again.';
	}
	const count = fieldErrorCount(form);
	return count > 1
		? 'Fix the highlighted fields and try again.'
		: 'Fix the highlighted field and try again.';
}

export function focusFirstInvalidControl(formElement: HTMLFormElement | null | undefined): boolean {
	if (!formElement) return false;
	const invalid = formElement.querySelector<HTMLElement>('[aria-invalid="true"]:not(:disabled)');
	if (invalid) {
		let details = invalid.closest('details');
		while (details) {
			details.open = true;
			details = details.parentElement?.closest('details') ?? null;
		}
		invalid.focus();
		return true;
	}
	const alert = formElement.querySelector<HTMLElement>('[role="alert"]');
	if (alert) {
		let details = alert.closest('details');
		while (details) {
			details.open = true;
			details = details.parentElement?.closest('details') ?? null;
		}
	}
	return false;
}
