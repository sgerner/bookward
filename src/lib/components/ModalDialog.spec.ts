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
	const selectControl = {
		tagName: 'SELECT',
		tabIndex: 0,
		focus: vi.fn(),
		getClientRects: () => [{ top: 64, left: 24, width: 120, height: 36 }],
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
		querySelectorAll: () => [initialButton, selectControl],
		contains: (target: unknown) => target === dialog || target === initialButton || target === selectControl,
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
	return { dialog, initialButton, selectControl, attributes, listeners };
}

describe('ModalDialog', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('renders nothing when closed and renders a native top-layer dialog with fallback classes when open on svelte/server', () => {
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
		expect(opened.body).toContain('data-[modal-fallback]:fixed');
		expect(opened.body).toContain('data-[modal-fallback]:z-[60]');
		expect(opened.body).toContain('Mark Read');
	});

	it('uses native cancel without window-capture Escape swallowing, and restores trigger focus once on destroy', () => {
		class FakeHTMLElement {
			isConnected = true;
			focus = vi.fn(() => {
				fakeDoc.activeElement = this;
			});
		}
		const trigger = new FakeHTMLElement();
		const windowListeners = new Map<string, Set<Listener>>();
		const fakeDoc = {
			activeElement: trigger as unknown,
			addEventListener: vi.fn(),
			removeEventListener: vi.fn(),
		};
		vi.stubGlobal('HTMLElement', FakeHTMLElement);
		vi.stubGlobal('document', fakeDoc);
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
		// Simulate showModal shifting activeElement inside the dialog
		dialog.showModal = vi.fn(() => {
			dialog.open = true;
			fakeDoc.activeElement = initialButton;
		});

		const action = attachModalDialog(dialog as unknown as HTMLDialogElement, { onClose });

		expect(dialog.showModal).toHaveBeenCalledTimes(1);
		expect(dialog.scrollTop).toBe(0);
		expect(initialButton.focus).toHaveBeenCalledWith({ preventScroll: true });
		// No window keydown listener registered when native showModal is available
		expect(windowListeners.get('keydown')?.size ?? 0).toBe(0);

		// Inside click should not close
		dialog.emit('click', { target: dialog, detail: 1, clientX: 100, clientY: 100 });
		expect(onClose).not.toHaveBeenCalled();

		// Backdrop click outside dialog rect should close
		dialog.emit('click', { target: dialog, detail: 1, clientX: 4, clientY: 4 });
		expect(onClose).toHaveBeenCalledTimes(1);

		// Native <dialog> cancel event closes cleanly
		const cancelPreventDefault = vi.fn();
		dialog.emit('cancel', { preventDefault: cancelPreventDefault });
		expect(cancelPreventDefault).toHaveBeenCalledTimes(1);
		expect(onClose).toHaveBeenCalledTimes(2);

		action.destroy();
		expect(dialog.close).toHaveBeenCalledTimes(1);
		expect(trigger.focus).toHaveBeenCalledTimes(1);
		expect(trigger.focus).toHaveBeenCalledWith({ preventScroll: true });
	});

	it('activates z-60 fixed modal fallback and bubble Escape/outside-pointerdown when showModal is absent', () => {
		const docListeners = new Map<string, Set<Listener>>();
		const { dialog, attributes } = createFakeDialog(false);
		vi.stubGlobal('document', {
			activeElement: null,
			querySelectorAll: () => [dialog],
			addEventListener: (type: string, listener: Listener) => {
				if (!docListeners.has(type)) docListeners.set(type, new Set());
				docListeners.get(type)!.add(listener);
			},
			removeEventListener: (type: string, listener: Listener) => {
				docListeners.get(type)?.delete(listener);
			},
		});

		const onClose = vi.fn();
		const action = attachModalDialog(dialog as unknown as HTMLDialogElement, { onClose });
		expect(attributes.get('open')).toBe('');
		expect(attributes.get('aria-modal')).toBe('true');
		expect(attributes.get('data-modal-fallback')).toBe('');

		// Escape with defaultPrevented (e.g. consumed by child control) is ignored
		dialog.emit('keydown', {
			key: 'Escape',
			defaultPrevented: true,
			isComposing: false,
			stopPropagation: vi.fn(),
		});
		expect(onClose).not.toHaveBeenCalled();

		// Unconsumed bubble Escape closes fallback dialog
		const stopPropagation = vi.fn();
		dialog.emit('keydown', {
			key: 'Escape',
			defaultPrevented: false,
			isComposing: false,
			stopPropagation,
		});
		expect(stopPropagation).toHaveBeenCalledTimes(1);
		expect(onClose).toHaveBeenCalledTimes(1);

		// Outside pointerdown closes fallback dialog
		for (const listener of docListeners.get('pointerdown') ?? []) {
			listener({ target: {} });
		}
		expect(onClose).toHaveBeenCalledTimes(2);

		action.destroy();
		expect(docListeners.get('pointerdown')?.size ?? 0).toBe(0);
	});
});
