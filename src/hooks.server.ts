import type { Handle } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';
import { timingSafeEqual } from 'node:crypto';

function equal(left: string, right: string) {
  const a=Buffer.from(left); const b=Buffer.from(right);
  return a.length===b.length && timingSafeEqual(a,b);
}

export function authorized(request: Request, username: string, password: string) {
  const header=request.headers.get('authorization') || '';
  let suppliedUser='', suppliedPassword='';
  if(header.startsWith('Basic ')) {
    try {
      const decoded=Buffer.from(header.slice(6),'base64').toString('utf8'); const separator=decoded.indexOf(':');
      if(separator>=0){ suppliedUser=decoded.slice(0,separator); suppliedPassword=decoded.slice(separator+1); }
    } catch { /* invalid header */ }
  }
  return equal(suppliedUser,username) && equal(suppliedPassword,password);
}

export function isPublicApiPath(pathname: string) {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname).replaceAll('\\', '/');
  } catch {
    return false;
  }
  if (decoded.split('/').some((segment) => segment === '.' || segment === '..')) {
    return false;
  }
  return decoded === '/api/v1' || decoded.startsWith('/api/v1/');
}

export function withSecurityHeaders(response: Response) {
  response.headers.set('x-content-type-options', 'nosniff');
  response.headers.set('x-frame-options', 'DENY');
  response.headers.set('referrer-policy', 'strict-origin-when-cross-origin');
  response.headers.set('permissions-policy', 'camera=(), geolocation=(), microphone=()');
  return response;
}

export const handle: Handle = async ({ event, resolve }) => {
  // The public API authenticates with a Bookward API token. Keep it separate
  // from the optional browser Basic-auth prompt so integrations do not need
  // to know deployment credentials.
  if (isPublicApiPath(event.url.pathname)) return withSecurityHeaders(await resolve(event));
  const password=env.AFTERWORD_AUTH_PASSWORD;
  if(!password) return withSecurityHeaders(await resolve(event));
  const username=env.AFTERWORD_AUTH_USERNAME || 'bookward';
  if(!authorized(event.request,username,password)) return withSecurityHeaders(new Response('Authentication required',{status:401,headers:{'www-authenticate':'Basic realm="Bookward", charset="UTF-8"','cache-control':'no-store'}}));
  return withSecurityHeaders(await resolve(event));
};
