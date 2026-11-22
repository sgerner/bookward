const DEFAULT_ACTION_ERROR_MESSAGE =
	'The request could not be completed. Your change was reverted.';

function payloadMessage(payload: unknown): string | null {
	if (!payload || typeof payload !== 'object') return null;
	for (const key of ['message', 'detail'] as const) {
		const value = (payload as Record<string, unknown>)[key];
		if (typeof value === 'string' && value.trim()) return value;
	}
	return null;
}

export function actionResultMessage(result: {
	type: string;
	data?: unknown;
	error?: unknown;
}): string {
	return (
		payloadMessage(result.data) ??
		payloadMessage(result.error) ??
		DEFAULT_ACTION_ERROR_MESSAGE
	);
}
