export function payloadErrorMessage(payload: unknown, fallback: string): string {
	if (payload && typeof payload === 'object') {
		const record = payload as { message?: unknown; detail?: unknown };
		if (typeof record.message === 'string' && record.message.trim()) {
			return record.message.trim();
		}
		if (typeof record.detail === 'string' && record.detail.trim()) {
			return record.detail.trim();
		}
	}
	return fallback;
}

export function actionErrorMessage(
	result: { type: string; data?: unknown; error?: unknown },
	fallback = 'The request could not be completed. Your change was reverted.'
): string {
	const fromData = payloadErrorMessage(result.data, '');
	if (fromData) return fromData;
	const fromError = payloadErrorMessage(result.error, '');
	if (fromError) return fromError;
	return fallback;
}
