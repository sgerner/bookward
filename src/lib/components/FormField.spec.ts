import { createRawSnippet } from 'svelte';
import { parse, type AST } from 'svelte/compiler';
import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import FormField, { type FormFieldControlAttrs } from './FormField.svelte';

function inputSnippet(extraAttrs = 'type="password" name="password"') {
	return createRawSnippet<[FormFieldControlAttrs]>((getAttrs) => ({
		render: () => {
			const attrs = getAttrs();
			const parts = [`id="${attrs.id}"`, extraAttrs];
			if (attrs['aria-describedby']) parts.push(`aria-describedby="${attrs['aria-describedby']}"`);
			if (attrs['aria-invalid']) parts.push(`aria-invalid="${attrs['aria-invalid']}"`);
			return `<input ${parts.join(' ')} />`;
		}
	}));
}

function collectText(nodes: AST.TemplateNode[]): string {
	let out = '';
	for (const node of nodes) {
		if (node.type === 'Text') out += node.data;
		else if ('fragment' in node && node.fragment?.nodes) {
			out += collectText(node.fragment.nodes);
		}
	}
	return out;
}

function findElements(nodes: AST.TemplateNode[], tagName: string): AST.RegularElement[] {
	const found: AST.RegularElement[] = [];
	for (const node of nodes) {
		if (node.type === 'RegularElement' && node.name === tagName) found.push(node);
		if ('fragment' in node && node.fragment?.nodes) {
			found.push(...findElements(node.fragment.nodes, tagName));
		}
	}
	return found;
}

describe('FormField', () => {
	it('pairs a sibling label with the control without nesting helper text inside the label', () => {
		const { body: html } = render(FormField, {
			props: {
				id: 'setup-password',
				label: 'Password',
				hint: 'Use at least 12 characters.',
				children: inputSnippet()
			}
		});
		const ast = parse(html, { modern: true });
		const labels = findElements(ast.fragment.nodes, 'label');

		expect(labels).toHaveLength(1);
		expect(collectText(labels[0]!.fragment.nodes)).toBe('Password');
		expect(html).toContain('<label for="setup-password"');
		expect(html).not.toMatch(/<label[^>]*>[\s\S]*Use at least 12 characters\.[\s\S]*<\/label>/);
		expect(html).toContain('id="setup-password"');
		expect(html).toContain('aria-describedby="setup-password-hint"');
		expect(html).not.toContain('aria-invalid');
		expect(html).toContain('<p id="setup-password-hint"');
	});

	it('combines error, hint, and external describedBy tokens and sets aria-invalid when invalid', () => {
		const { body: html } = render(FormField, {
			props: {
				id: 'restore-confirm',
				label: 'Type RESTORE to confirm',
				hint: 'Case-sensitive confirmation.',
				error: 'Type RESTORE to confirm replacing the current database.',
				describedBy: ['restore-backup-warning', 'restore-confirm-hint'],
				children: inputSnippet('type="text" name="confirm"')
			}
		});

		expect(html).toContain(
			'aria-describedby="restore-confirm-error restore-confirm-hint restore-backup-warning"'
		);
		expect(html).toContain('aria-invalid="true"');
		expect(html).toContain(
			'<p id="restore-confirm-error" class="text-xs font-medium leading-5 text-error-600-400">Type RESTORE to confirm replacing the current database.</p>'
		);
	});

	it('renders optional screen-reader label context inside the sibling label and omits empty aria attributes', () => {
		const { body: html } = render(FormField, {
			props: {
				id: 'reset-password-acct-7',
				label: 'Set password',
				srLabel: 'for Alice',
				children: inputSnippet()
			}
		});
		const ast = parse(html, { modern: true });
		const labels = findElements(ast.fragment.nodes, 'label');

		expect(labels).toHaveLength(1);
		expect(collectText(labels[0]!.fragment.nodes)).toBe('Set password for Alice');
		expect(html).toContain('<span class="sr-only"> for Alice</span>');
		expect(html).not.toContain('aria-describedby');
		expect(html).not.toContain('aria-invalid');
	});

	it('marks controls aria-invalid and links an external error summary when invalid is true without per-field error text', () => {
		const { body: html } = render(FormField, {
			props: {
				id: 'login-username',
				label: 'Username',
				invalid: true,
				describedBy: 'login-error-summary',
				children: inputSnippet('type="text" name="username"')
			}
		});

		expect(html).toContain('id="login-username"');
		expect(html).toContain('aria-describedby="login-error-summary"');
		expect(html).toContain('aria-invalid="true"');
		expect(html).not.toContain('id="login-username-error"');
	});
});
