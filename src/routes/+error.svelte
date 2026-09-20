<script lang="ts">
  import { page } from '$app/state';
  import { fly, scale } from 'svelte/transition';
  import { ArrowRight } from '@lucide/svelte';
  import bookwardMark from '$lib/assets/bookward-mark.svg';

  function motionDuration(duration: number) {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return duration;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : duration;
  }
</script>

<svelte:head>
  <title>{page.status} — Bookward</title>
</svelte:head>

<main in:fly={{ y: 18, duration: motionDuration(320) }} class="mx-auto grid min-h-dvh max-w-xl content-center gap-6 px-6 py-12">
  <a href="/" class="flex items-center gap-3 text-xl font-semibold"><span in:scale={{ duration: motionDuration(180) }} class="grid size-11 overflow-hidden rounded-2xl shadow-lg shadow-primary-500/20"><img src={bookwardMark} alt="" class="size-full object-cover" aria-hidden="true" /></span>Bookward</a>
  <div in:scale={{ duration: motionDuration(280) }} class="card preset-tonal-surface space-y-5 p-6 sm:p-10">
    <span in:fly={{ x: -8, duration: motionDuration(180) }} class="badge preset-tonal-warning">{page.status}</span>
    <h1 class="h2">{page.status === 503 ? 'A short intermission' : 'Something went wrong'}</h1>
    <p class="text-surface-700-300 leading-relaxed">{page.status === 503 ? 'We couldn’t reach your book library. Please try again in a moment.' : page.error?.message ?? 'We couldn’t load this page.'}</p>
    <a class="btn preset-filled-primary-500 min-h-11" href="/">Try again<ArrowRight size={16} /></a>
    {#if page.status === 503}<details class="text-sm text-surface-700-300"><summary class="cursor-pointer py-2">Server administrator?</summary><p class="pt-2">Check that the recommendation service is running and reachable from the web server.</p></details>{/if}
  </div>
</main>
