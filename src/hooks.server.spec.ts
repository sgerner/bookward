import { describe, expect, it } from 'vitest';
import { authorized, isPublicApiPath, withSecurityHeaders } from './hooks.server';

describe('authentication boundary', () => {
  it('requires configured basic authentication and accepts colons in passwords', async () => {
    expect(authorized(new Request('http://afterword.test'), 'reader', 'long:secret')).toBe(false);
    const authorization = `Basic ${Buffer.from('reader:long:secret').toString('base64')}`;
    expect(authorized(new Request('http://afterword.test', { headers: { authorization } }), 'reader', 'long:secret')).toBe(true);
  });

  it('recognizes only the versioned API paths as token-authenticated routes', () => {
    expect(isPublicApiPath('/api/v1')).toBe(true);
    expect(isPublicApiPath('/api/v1/recommendations')).toBe(true);
    expect(isPublicApiPath('/api/v10/recommendations')).toBe(false);
    expect(isPublicApiPath('/settings')).toBe(false);
  });

  it('adds baseline browser security headers to every response', () => {
    const response = withSecurityHeaders(new Response('ok'));

    expect(response.headers.get('x-content-type-options')).toBe('nosniff');
    expect(response.headers.get('x-frame-options')).toBe('DENY');
    expect(response.headers.get('referrer-policy')).toBe('strict-origin-when-cross-origin');
    expect(response.headers.get('permissions-policy')).toBe('camera=(), geolocation=(), microphone=()');
  });
});
