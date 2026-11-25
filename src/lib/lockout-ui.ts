export function formatWaitDuration(seconds?: number): string {
	const clamped =
		typeof seconds === 'number' && Number.isFinite(seconds) && seconds > 0
			? Math.ceil(seconds)
			: 900;
	if (clamped >= 60) {
		const minutes = Math.ceil(clamped / 60);
		return `${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
	}
	return `${clamped} ${clamped === 1 ? 'second' : 'seconds'}`;
}
