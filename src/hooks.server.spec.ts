import { describe, expect, it } from 'vitest';
import { authorized } from './hooks.server';

describe('authentication boundary', () => {
  it('requires configured basic authentication and accepts colons in passwords', async () => {
    expect(authorized(new Request('http://afterword.test'), 'reader', 'long:secret')).toBe(false);
    const authorization = `Basic ${Buffer.from('reader:long:secret').toString('base64')}`;
    expect(authorized(new Request('http://afterword.test', { headers: { authorization } }), 'reader', 'long:secret')).toBe(true);
  });
});
