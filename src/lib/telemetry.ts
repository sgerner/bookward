export type TelemetryEvent = {
  event_key: string;
  candidate_id: number;
  event_type:
    | "visible"
    | "detail_open"
    | "source_open"
    | "librarr_search"
    | "librarr_import";
  run_id?: string;
  value?: number;
  source?: string;
  metadata?: Record<string, unknown>;
};

type TelemetryTransport = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

type BeaconTransport = (url: string, data?: BodyInit | null) => boolean;

type TelemetryOptions = {
  fetchImpl?: TelemetryTransport;
  beaconImpl?: BeaconTransport;
  randomUUID?: () => string;
  flushDelayMs?: number;
  maxBatchSize?: number;
};

const DEFAULT_BATCH_SIZE = 20;
const MAX_QUEUE_SIZE = 100;

function fallbackId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

function eventId(randomUUID: () => string) {
  return `ui-${randomUUID()}`;
}

export function createTelemetryClient(options: TelemetryOptions = {}) {
  const send = options.fetchImpl ?? globalThis.fetch?.bind(globalThis);
  const beacon =
    options.beaconImpl ??
    (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function"
      ? navigator.sendBeacon.bind(navigator)
      : undefined);
  const makeId = options.randomUUID ?? (() => globalThis.crypto?.randomUUID?.() ?? fallbackId());
  const flushDelayMs = options.flushDelayMs ?? 150;
  const maxBatchSize = Math.max(1, Math.min(options.maxBatchSize ?? DEFAULT_BATCH_SIZE, MAX_QUEUE_SIZE));
  let queue: TelemetryEvent[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  let destroyed = false;

  function schedule() {
    if (timer || destroyed) return;
    timer = setTimeout(() => {
      timer = undefined;
      void flush();
    }, flushDelayMs);
  }

  function enqueue(event: Omit<TelemetryEvent, "event_key"> & { event_key?: string }) {
    if (destroyed || !Number.isInteger(event.candidate_id) || event.candidate_id <= 0) return;
    const next: TelemetryEvent = {
      ...event,
      event_key: event.event_key || eventId(makeId),
      source: event.source || "ui",
    };
    queue = [...queue.slice(-(MAX_QUEUE_SIZE - 1)), next];
    if (queue.length >= maxBatchSize) void flush();
    else schedule();
  }

  async function flush({ beacon: preferBeacon = false } = {}) {
    if (!queue.length) return;
    const batch = queue.splice(0, maxBatchSize);
    const body = JSON.stringify({ events: batch });
    if (preferBeacon && beacon) {
      try {
        if (beacon("/api/telemetry", new Blob([body], { type: "application/json" }))) {
          if (queue.length && !destroyed) schedule();
          return;
        }
      } catch {
        // Fall through to fetch when sendBeacon is unavailable or rejected.
      }
    }
    if (!send) return;
    try {
      const response = await send("/api/telemetry", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body,
        keepalive: preferBeacon,
      });
      if (!response.ok) {
        // The current batch is intentionally dropped; events are advisory.
      }
    } catch {
      // Telemetry is best effort. Dropping a failed batch must never block the
      // reader or turn a recommendation action into a visible error.
    }
    if (queue.length && !destroyed) schedule();
  }

  function destroy() {
    destroyed = true;
    if (timer) clearTimeout(timer);
    timer = undefined;
    queue = [];
  }

  return { enqueue, flush, destroy };
}

export type TelemetryClient = ReturnType<typeof createTelemetryClient>;
