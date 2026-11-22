import { createRawSnippet } from 'svelte';
import { render } from 'svelte/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import HeaderPopover, { attachHeaderPopover } from './HeaderPopover.svelte';

const menuChildren = createRawSnippet(() => ({
	render: () => '<button type="submit">Sign out</button>',
}));

type Listener = (event: unknown) => void;

describe('HeaderPopover', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
	});

	it('renders manual popover with viewport-fixed placement classes on svelte/server', () => {
		const closed = render(HeaderPopover, {
			props: { id: 'profile-menu-panel', open: false, children: menuChildren },
		});
		expect(closed.body).not.toContain('id="profile-menu-panel"');

		const opened = render(HeaderPopover, {
			props: {
				id: 'profile-menu-panel',
				open: true,
				ariaLabel: 'Profile menu',
				children: menuChildren,
			},
		});
		expect(opened.body).toContain('id="profile-menu-panel"');
		expect(opened.body).toContain('popover="manual"');
		expect(opened.body).toContain('fixed');
		expect(opened.body).toContain('right-4');
		expect(opened.body).toContain('top-[4.25rem]');
		expect(opened.body).toContain('left-auto');
		expect(opened.body).toContain('bottom-auto');
		expect(opened.body).toContain('m-0');
		expect(opened.body).toContain('max-h-[calc(100dvh-4.5rem)]');
		expect(opened.body).toContain('Sign out');
	});

	it('opens via showPopover, guards light-dismissal while nested dialog is open, and hides on destroy', () => {
		const docListeners = new Map<string, Set<Listener>>();
		const winListeners = new Map<string, Set<Listener>>();
		vi.stubGlobal('document', {
			addEventListener: (type: string, listener: Listener) => {
				if (!docListeners.has(type)) docListeners.set(type, new Set());
				docListeners.get(type)!.add(listener);
			},
			removeEventListener: (type: string, listener: Listener) => {
				docListeners.get(type)?.delete(listener);
			},
		});
		vi.stubGlobal('window', {
			addEventListener: (type: string, listener: Listener) => {
				if (!winListeners.has(type)) winListeners.set(type, new Set());
				winListeners.get(type)!.add(listener);
			},
			removeEventListener: (type: string, listener: Listener) => {
				winListeners.get(type)?.delete(listener);
			},
		});

		let popoverOpen = false;
		let nestedDialogOpen = true;
		const outsideNode = {};
		const insideNode = {};
		const triggerNode = {
			contains: (target: unknown) => target === triggerNode,
		};
		const popoverNode = {
			matches: (selector: string) => selector === ':popover-open' && popoverOpen,
			showPopover: vi.fn(() => {
				popoverOpen = true;
			}),
			hidePopover: vi.fn(() => {
				popoverOpen = false;
			}),
			querySelector: (selector: string) => (selector === 'dialog[open]' && nestedDialogOpen ? {} : null),
			contains: (target: unknown) => target === popoverNode || target === insideNode,
		};

		const onClose = vi.fn();
		const action = attachHeaderPopover(popoverNode as unknown as HTMLElement, {
			onClose,
			trigger: triggerNode as unknown as HTMLElement,
		});

		expect(popoverNode.showPopover).toHaveBeenCalledTimes(1);

		// While nested ModalDialog (ThemePicker) is open, outside pointerdown and Escape must NOT dismiss parent popover
		for (const listener of docListeners.get('pointerdown') ?? []) {
			listener({ target: outsideNode });
		}
		for (const listener of winListeners.get('keydown') ?? []) {
			listener({ key: 'Escape', preventDefault: vi.fn(), stopPropagation: vi.fn() });
		}
		expect(onClose).not.toHaveBeenCalled();

		// Once nested dialog closes, outside pointerdown and Escape dismiss cleanly
		nestedDialogOpen = false;
		for (const listener of docListeners.get('pointerdown') ?? []) {
			listener({ target: triggerNode });
		}
		expect(onClose).not.toHaveBeenCalled();

		for (const listener of docListeners.get('pointerdown') ?? []) {
			listener({ target: outsideNode });
		}
		expect(onClose).toHaveBeenCalledWith(false);

		const preventDefault = vi.fn();
		const stopPropagation = vi.fn();
		for (const listener of winListeners.get('keydown') ?? []) {
			listener({ key: 'Escape', preventDefault, stopPropagation });
		}
		expect(onClose).toHaveBeenCalledWith(true);

		action.destroy();
		expect(popoverNode.hidePopover).toHaveBeenCalledTimes(1);
	});

	it('guards showPopover and hidePopover when running without Popover API methods', () => {
		const fallbackNode = {} as HTMLElement;
		expect(() => {
			const action = attachHeaderPopover(fallbackNode);
			action.destroy();
		}).not.toThrow();
	});
});
