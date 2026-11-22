<script lang="ts">
  import { page } from '$app/state';
  import { fly, scale } from 'svelte/transition';
  import { ArrowRight } from '@lucide/svelte';
  import bookwardMark from '#lib/assets/bookward-mark.svg';

  function motionDuration(duration: number) {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return duration;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : duration;
  }
</script>

<svelte:head>
  <title>{page.status} — Bookward</title>
</svelte:head>

<a href="#main-content" class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:btn focus:preset-filled-primary-500">Skip to content</a>
<main id="main-content" tabindex="-1" in:fly={{ y: 18, duration: motionDuration(320) }} class="mx-auto grid min-h-dvh max-w-xl content-center gap-6 px-6 py-12">
  <a href="/" class="flex w-fit items-center gap-3 rounded-xl text-xl font-semibold focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-primary-500"><span in:scale={{ duration: motionDuration(180) }} class="grid size-11 overflow-hidden rounded-2xl shadow-lg shadow-primary-500/20"><img src={bookwardMark} alt="" width="128" height="128" class="size-full object-cover" aria-hidden="true" /></span>Bookward</a>
  <div in:scale={{ duration: motionDuration(280) }} class="card preset-tonal-surface space-y-5 p-6 sm:p-10">
    <span in:fly={{ x: -8, duration: motionDuration(180) }} class="badge preset-tonal-warning">{page.status}</span>
    <h1 class="h2">{page.status === 403 ? 'Request blocked' : page.status === 503 ? 'A short intermission' : page.status === 404 ? 'Page not found' : page.status === 413 ? 'File too large' : 'Something went wrong'}</h1>
    <p class="text-surface-700-300 leading-relaxed">{page.status === 403 ? 'Bookward couldn’t verify that this action came from the site’s configured address. Reload the page and try again.' : page.status === 503 ? 'We couldn’t reach your book library. Please try again in a moment.' : page.status === 404 ? 'That page doesn’t exist or may have moved. Return to your Bookward home to continue browsing.' : page.status === 413 ? 'Goodreads CSV exports must be 10 MB or smaller (up to 20,000 books). Choose a smaller CSV file and try again.' : page.error?.message ?? 'We couldn’t load this page.'}</p>
    <a class="btn preset-filled-primary-500 min-h-11 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500" href={page.status === 413 ? '/?view=settings' : '/'}>{page.status === 404 ? 'Go to Bookward' : page.status === 413 ? 'Back to Settings' : 'Try again'}<ArrowRight size={16} /></a>
    {#if page.status === 403}<details open class="text-sm text-surface-700-300"><summary class="cursor-pointer rounded-lg py-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500">Server administrator?</summary><p class="pt-2 leading-relaxed">Set <code>ORIGIN</code> to the exact public URL opened in the browser, or configure <code>PROTOCOL_HEADER</code> and <code>HOST_HEADER</code> when Bookward runs behind a trusted reverse proxy, then restart the web container.</p></details>{:else if page.status === 503}<details class="text-sm text-surface-700-300"><summary class="cursor-pointer rounded-lg py-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500">Server administrator?</summary><p class="pt-2">Check that the recommendation service is running and reachable from the web server.</p></details>{/if}
  </div>
</main>
