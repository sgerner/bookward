import { describe, expect, it, vi } from 'vitest';
import { nextRovingIndex, rovingFocus } from './roving-focus';

type FakeRadio = {
	attributes: Map<string, string>;
	getAttribute: (name: string) => string | null;
	setAttribute: (name: string, value: string) => void;
	contains: (target: unknown) => boolean;
	focus: ReturnType<typeof vi.fn>;
	scrollIntoView: ReturnType<typeof vi.fn>;
};

function createFakeRadio(checked = false): FakeRadio {
	const attributes = new Map<string, string>([
		['role', 'radio'],
		['aria-checked', checked ? 'true' : 'false'],
	]);
	const radio: FakeRadio = {
		attributes,
		getAttribute: (name) => attributes.get(name) ?? null,
		setAttribute: (name, value) => {
			attributes.set(name, value);
		},
		contains: (target) => target === radio,
		focus: vi.fn(),
		scrollIntoView: vi.fn(),
	};
	return radio;
}

function createFakeContainer(radios: FakeRadio[]) {
	const listeners = new Map<string, Set<(event: unknown) => void>>();
	return {
		node: {
			querySelectorAll: () => radios,
			contains: (target: unknown) => radios.some((radio) => radio === target),
			addEventListener: (type: string, handler: (event: unknown) => void) => {
				if (!listeners.has(type)) listeners.set(type, new Set());
				listeners.get(type)!.add(handler);
			},
			removeEventListener: (type: string, handler: (event: unknown) => void) => {
				listeners.get(type)?.delete(handler);
			},
		} as unknown as HTMLElement,
		dispatch(type: string, event: Record<string, unknown>) {
			for (const handler of listeners.get(type) ?? []) {
				handler(event);
			}
		},
		listenerCount(type: string) {
			return listeners.get(type)?.size ?? 0;
		},
	};
}

describe('nextRovingIndex', () => {
	it('navigates 1D radiogroups with wrapping and Home/End support', () => {
		expect(nextRovingIndex(0, 5, 'ArrowRight', 1)).toBe(1);
		expect(nextRovingIndex(0, 5, 'ArrowDown', 1)).toBe(1);
		expect(nextRovingIndex(4, 5, 'ArrowRight', 1)).toBe(0);
		expect(nextRovingIndex(0, 5, 'ArrowLeft', 1)).toBe(4);
		expect(nextRovingIndex(0, 5, 'ArrowUp', 1)).toBe(4);
		expect(nextRovingIndex(3, 5, 'Home', 1)).toBe(0);
		expect(nextRovingIndex(1, 5, 'End', 1)).toBe(4);
		expect(nextRovingIndex(1, 5, 'Enter', 1)).toBeNull();
		expect(nextRovingIndex(0, 0, 'ArrowRight', 1)).toBeNull();
	});

	it('navigates 2D grids across rows and columns with wrapping', () => {
		expect(nextRovingIndex(0, 6, 'ArrowDown', 2)).toBe(2);
		expect(nextRovingIndex(5, 6, 'ArrowDown', 2)).toBe(1);
		expect(nextRovingIndex(0, 6, 'ArrowUp', 2)).toBe(4);
		expect(nextRovingIndex(1, 6, 'ArrowRight', 2)).toBe(2);
		expect(nextRovingIndex(0, 6, 'ArrowLeft', 2)).toBe(5);
	});
});

describe('rovingFocus action', () => {
	it('maintains a single tabindex=0 stop and moves focus across radios', () => {
		const radios = Array.from({ length: 5 }, (_, idx) => createFakeRadio(idx === 1));
		const { node, dispatch, listenerCount } = createFakeContainer(radios);
		const onMove = vi.fn();
		const onActiveIndexChange = vi.fn();

		const handle = rovingFocus(node, {
			columns: 1,
			selectedIndex: 1,
			onMove,
			onActiveIndexChange,
		});

		expect(radios.map((r) => r.getAttribute('tabindex'))).toEqual(['-1', '0', '-1', '-1', '-1']);

		const preventDefault = vi.fn();
		dispatch('keydown', { key: 'ArrowRight', target: radios[1], preventDefault });

		expect(preventDefault).toHaveBeenCalledTimes(1);
		expect(radios[2].focus).toHaveBeenCalledTimes(1);
		expect(radios[2].scrollIntoView).toHaveBeenCalledWith({ block: 'nearest' });
		expect(onMove).toHaveBeenCalledWith(2);
		expect(onActiveIndexChange).toHaveBeenCalledWith(2);
		expect(radios.map((r) => r.getAttribute('tabindex'))).toEqual(['-1', '-1', '0', '-1', '-1']);

		handle.update({
			columns: 1,
			selectedIndex: 4,
			onMove,
			onActiveIndexChange,
		});
		expect(radios.map((r) => r.getAttribute('tabindex'))).toEqual(['-1', '-1', '-1', '-1', '0']);

		handle.destroy();
		expect(listenerCount('keydown')).toBe(0);
		expect(listenerCount('focusin')).toBe(0);
	});

	it('clears optional ratings on Backspace and Delete while keeping one focusable stop', () => {
		const radios = Array.from({ length: 5 }, () => createFakeRadio(false));
		const { node, dispatch } = createFakeContainer(radios);
		const onClear = vi.fn();

		rovingFocus(node, {
			columns: 1,
			selectedIndex: -1,
			onClear,
		});

		expect(radios.map((r) => r.getAttribute('tabindex'))).toEqual(['0', '-1', '-1', '-1', '-1']);

		dispatch('focusin', { target: radios[2] });
		expect(radios.map((r) => r.getAttribute('tabindex'))).toEqual(['-1', '-1', '0', '-1', '-1']);

		const preventBackspace = vi.fn();
		dispatch('keydown', { key: 'Backspace', target: radios[2], preventDefault: preventBackspace });
		expect(preventBackspace).toHaveBeenCalledTimes(1);
		expect(onClear).toHaveBeenCalledTimes(1);
		expect(radios.filter((r) => r.getAttribute('tabindex') === '0')).toHaveLength(1);

		const preventDelete = vi.fn();
		dispatch('keydown', { key: 'Delete', target: radios[2], preventDefault: preventDelete });
		expect(preventDelete).toHaveBeenCalledTimes(1);
		expect(onClear).toHaveBeenCalledTimes(2);
	});
});
