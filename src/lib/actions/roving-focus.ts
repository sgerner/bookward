export type RovingFocusOptions = {
	columns?: number;
	selectedIndex?: number;
	itemSelector?: string;
	onMove?: (index: number) => void;
	onClear?: () => void;
	onActiveIndexChange?: (index: number) => void;
};

const DEFAULT_ITEM_SELECTOR = '[role="radio"]:not(:disabled)';

/**
 * Compute the next roving focus index for 1D and 2D radiogroups.
 * Returns `null` when `key` is not a roving navigation key or `count <= 0`.
 */
export function nextRovingIndex(
	currentIndex: number,
	count: number,
	key: string,
	columns = 1,
): number | null {
	if (count <= 0) return null;
	const cols = Math.max(1, Math.trunc(columns));
	const current = ((Math.trunc(currentIndex) % count) + count) % count;

	if (key === 'Home') return 0;
	if (key === 'End') return count - 1;

	// Single-row or 1D group: both horizontal and vertical arrows step sequentially.
	if (cols === 1 || count <= cols) {
		if (key === 'ArrowRight' || key === 'ArrowDown') return (current + 1) % count;
		if (key === 'ArrowLeft' || key === 'ArrowUp') return (current - 1 + count) % count;
		return null;
	}

	// Multi-row 2D grid.
	if (key === 'ArrowRight') return (current + 1) % count;
	if (key === 'ArrowLeft') return (current - 1 + count) % count;
	if (key === 'ArrowDown') {
		return current + cols < count ? current + cols : current % cols;
	}
	if (key === 'ArrowUp') {
		if (current - cols >= 0) return current - cols;
		const col = current % cols;
		const lastRowStart = Math.floor((count - 1) / cols) * cols;
		const candidate = lastRowStart + col;
		return candidate < count ? candidate : candidate - cols;
	}

	return null;
}

export function rovingFocus(node: HTMLElement, initialOptions: RovingFocusOptions = {}) {
	let options = initialOptions;
	let lastSelectedIndex = options.selectedIndex;

	const getItems = (): HTMLElement[] =>
		Array.from(node.querySelectorAll<HTMLElement>(options.itemSelector ?? DEFAULT_ITEM_SELECTOR));

	const resolveInitialIndex = (items: HTMLElement[]): number => {
		if (items.length === 0) return -1;
		if (
			options.selectedIndex !== undefined &&
			options.selectedIndex >= 0 &&
			options.selectedIndex < items.length
		) {
			return options.selectedIndex;
		}
		const checkedIndex = items.findIndex((item) => item.getAttribute('aria-checked') === 'true');
		return checkedIndex >= 0 ? checkedIndex : 0;
	};

	let activeIndex = resolveInitialIndex(getItems());

	const syncTabStops = (items = getItems()) => {
		if (items.length === 0) {
			activeIndex = -1;
			return;
		}
		if (activeIndex < 0 || activeIndex >= items.length) {
			activeIndex = resolveInitialIndex(items);
		}
		for (let index = 0; index < items.length; index += 1) {
			items[index].setAttribute('tabindex', index === activeIndex ? '0' : '-1');
		}
	};

	const setActiveIndex = (nextIndex: number, items = getItems()) => {
		if (items.length === 0) return;
		const clamped = Math.max(0, Math.min(items.length - 1, nextIndex));
		activeIndex = clamped;
		syncTabStops(items);
		options.onActiveIndexChange?.(clamped);
	};

	const findTargetIndex = (items: HTMLElement[], target: EventTarget | null): number => {
		if (!target) return -1;
		return items.findIndex(
			(item) =>
				item === target || (typeof item.contains === 'function' && item.contains(target as Node)),
		);
	};

	const onKeydown = (event: KeyboardEvent) => {
		const items = getItems();
		if (items.length === 0) return;

		const targetIndex = findTargetIndex(items, event.target);
		const current = targetIndex >= 0 ? targetIndex : Math.max(0, activeIndex);

		if ((event.key === 'Backspace' || event.key === 'Delete') && options.onClear) {
			event.preventDefault();
			setActiveIndex(current, items);
			options.onClear();
			return;
		}

		const next = nextRovingIndex(current, items.length, event.key, options.columns ?? 1);
		if (next === null) return;

		event.preventDefault();
		setActiveIndex(next, items);
		const nextItem = items[next];
		nextItem.focus();
		if (typeof nextItem.scrollIntoView === 'function') {
			nextItem.scrollIntoView({ block: 'nearest' });
		}
		options.onMove?.(next);
	};

	const onFocusIn = (event: FocusEvent) => {
		const items = getItems();
		const targetIndex = findTargetIndex(items, event.target);
		if (targetIndex >= 0 && targetIndex !== activeIndex) {
			setActiveIndex(targetIndex, items);
		}
	};

	syncTabStops();
	node.addEventListener('keydown', onKeydown);
	node.addEventListener('focusin', onFocusIn);

	return {
		update(nextOptions: RovingFocusOptions = {}) {
			const selectionChanged = nextOptions.selectedIndex !== lastSelectedIndex;
			options = nextOptions;
			lastSelectedIndex = nextOptions.selectedIndex;
			const items = getItems();

			if (selectionChanged) {
				if (
					nextOptions.selectedIndex !== undefined &&
					nextOptions.selectedIndex >= 0 &&
					nextOptions.selectedIndex < items.length
				) {
					activeIndex = nextOptions.selectedIndex;
				} else if (
					typeof document !== 'undefined' &&
					document.activeElement &&
					typeof node.contains === 'function' &&
					!node.contains(document.activeElement)
				) {
					activeIndex = 0;
				}
			}

			syncTabStops(items);
		},
		destroy() {
			node.removeEventListener('keydown', onKeydown);
			node.removeEventListener('focusin', onFocusIn);
		},
	};
}
