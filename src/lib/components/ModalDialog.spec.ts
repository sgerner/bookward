import { createRawSnippet } from 'svelte';
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ModalDialog, { attachModalDialog } from './ModalDialog.svelte';

const dialogChildren = createRawSnippet(() => ({
	render: () => '<div><h2 id="dialog-title">Mark Read</h2><button type="button" data-modal-initial-focus>Close</button></div>',
}));

type Listener = (event: unknown) => void;

function createFakeDialog(withNativeShowModal = true) {
	const listeners = new Map<string, Set<Listener>>();
	const attributes = new Map<string, string>();
	const initialButton = {
		tabIndex: 0,
		focus: vi.fn(),
		getClientRects: () => [{ top: 24, left: 24, width: 32, height: 32 }],
		closest: () => null,
	};
	const dialog = {
		open: false,
		scrollTop: 92,
		showModal: withNativeShowModal
			? vi.fn(() => {
					dialog.open = true;
				})
			: undefined,
		close: withNativeShowModal
			? vi.fn(() => {
					dialog.open = false;
				})
			: undefined,
		hasAttribute: (name: string) => attributes.has(name),
		setAttribute: (name: string, value: string) => {
			attributes.set(name, value);
		},
		querySelector: (selector: string) => (selector === '[data-modal-initial-focus]' ? initialButton : null),
		querySelectorAll: () => [initialButton],
		contains: (target: unknown) => target === dialog || target === initialButton,
		getBoundingClientRect: () => ({ top: 16, left: 16, right: 304, bottom: 240, width: 288, height: 224 }),
		addEventListener: (type: string, listener: Listener) => {
			if (!listeners.has(type)) listeners.set(type, new Set());
			listeners.get(type)!.add(listener);
		},
		removeEventListener: (type: string, listener: Listener) => {
			listeners.get(type)?.delete(listener);
		},
		emit(type: string, event: unknown) {
			for (const listener of listeners.get(type) ?? []) listener(event);
		},
	};
	return { dialog, initialButton, attributes };
}

describe('ModalDialog', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('renders nothing when closed and renders a native top-layer dialog when open on svelte/server', () => {
		const closed = render(ModalDialog, {
			props: { open: false, children: dialogChildren },
		});
		expect(closed.body).not.toContain('<dialog');

		const opened = render(ModalDialog, {
			props: {
				id: 'mark-read-dialog',
				open: true,
				ariaLabelledby: 'dialog-title',
				ariaDescribedby: 'dialog-desc',
				class: 'max-w-xl',
				children: dialogChildren,
			},
		});
		expect(opened.body).toContain('<dialog');
		expect(opened.body).toContain('id="mark-read-dialog"');
		expect(opened.body).toContain('aria-labelledby="dialog-title"');
		expect(opened.body).toContain('aria-describedby="dialog-desc"');
		expect(opened.body).toContain('m-auto');
		expect(opened.body).toContain('max-h-[calc(100dvh-2rem)]');
		expect(opened.body).toContain('overflow-y-auto');
		expect(opened.body).toContain('Mark Read');
	});

	it('opens via showModal, resets scrollTop, focuses initial target, and restores trigger focus on destroy', () => {
		class FakeHTMLElement {
			isConnected = true;
			focus = vi.fn();
		}
		const trigger = new FakeHTMLElement();
		const windowListeners = new Map<string, Set<Listener>>();
		vi.stubGlobal('HTMLElement', FakeHTMLElement);
		vi.stubGlobal('document', {
			activeElement: trigger,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		});
		vi.stubGlobal('window', {
			addEventListener: (type: string, listener: Listener) => {
				if (!windowListeners.has(type)) windowListeners.set(type, new Set());
				windowListeners.get(type)!.add(listener);
			},
			removeEventListener: (type: string, listener: Listener) => {
				windowListeners.get(type)?.delete(listener);
			},
		});
		vi.stubGlobal('getComputedStyle', () => ({ visibility: 'visible' }));

		const onClose = vi.fn();
		const { dialog, initialButton } = createFakeDialog(true);
		const action = attachModalDialog(dialog as unknown as HTMLDialogElement, { onClose });

		expect(dialog.showModal).toHaveBeenCalledTimes(1);
		expect(dialog.scrollTop).toBe(0);
		expect(initialButton.focus).toHaveBeenCalledWith({ preventScroll: true });

		// Inside click should not close
		dialog.emit('click', { target: dialog, detail: 1, clientX: 100, clientY: 100 });
		expect(onClose).not.toHaveBeenCalled();

		// Backdrop click outside dialog rect should close
		dialog.emit('click', { target: dialog, detail: 1, clientX: 4, clientY: 4 });
		expect(onClose).toHaveBeenCalledTimes(1);

		// Escape stops propagation so parent popovers stay open
		const preventDefault = vi.fn();
		const stopPropagation = vi.fn();
		for (const listener of windowListeners.get('keydown') ?? []) {
			listener({ key: 'Escape', preventDefault, stopPropagation });
		}
		expect(preventDefault).toHaveBeenCalledTimes(1);
		expect(stopPropagation).toHaveBeenCalledTimes(1);
		expect(onClose).toHaveBeenCalledTimes(2);

		action.destroy();
		expect(dialog.close).toHaveBeenCalledTimes(1);
		expect(trigger.focus).toHaveBeenCalledWith({ preventScroll: true });
	});

	it('guards showModal and close when running without native HTMLDialogElement methods', () => {
		const { dialog, attributes } = createFakeDialog(false);
		expect(() => {
			const action = attachModalDialog(dialog as unknown as HTMLDialogElement, {});
			action.destroy();
		}).not.toThrow();
		expect(attributes.has('open')).toBe(true);
	});
});
