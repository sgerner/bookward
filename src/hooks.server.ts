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

export const handle: Handle = async ({ event, resolve }) => {
  const password=env.AFTERWORD_AUTH_PASSWORD;
  if(!password) return resolve(event);
  const username=env.AFTERWORD_AUTH_USERNAME || 'bookward';
  if(!authorized(event.request,username,password)) return new Response('Authentication required',{status:401,headers:{'www-authenticate':'Basic realm="Bookward", charset="UTF-8"','cache-control':'no-store'}});
  return resolve(event);
};
