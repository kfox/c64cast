import type { LogLine, PerfSystem, Role, SessionStatus, StateFrame } from "./types";

/** How many log lines the browser keeps. The daemon's own buffer holds 500 and
 *  hands a new connection the last 200; matching its ceiling means a console
 *  left open for a day shows the same window as one just opened. */
const LOG_LIMIT = 500;

const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 8000;

function socketUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/ws`;
}

/**
 * The live state of the host, as one reactive object the whole app reads.
 *
 * Deliberately a single socket rather than polling: the daemon pushes about
 * three times a second and every screen wants the same frame, so a second
 * consumer costs nothing. The frame is kept whole (`frame`) as well as split
 * into the pieces this screen uses — the performance surface arrives in the
 * same payload, and the screens that render it should not need a second feed.
 */
export class Console {
  /** Whether the socket is up *right now*. Distinct from having state: a
   *  console that just lost its connection should keep showing the last thing
   *  it knew, greyed, rather than blanking. */
  connected = $state(false);
  /** Set once the first frame lands, and never cleared — "we have never heard
   *  from the host" and "we heard from it a second ago" want different UI. */
  ready = $state(false);
  session = $state<SessionStatus | null>(null);
  role = $state<Role>(null);
  /** Set by a screen right before it asks the host to start or switch to a
   *  config, so the shell can jump to the Live tab once the show actually
   *  comes up. A viewer just watching the feed, or a start this browser
   *  didn't ask for, should not be yanked between tabs by it — so this is
   *  local to the browser that set it, not part of the state feed. */
  expectingStart = $state(false);
  log = $state<LogLine[]>([]);
  frame = $state<StateFrame>({});

  #socket: WebSocket | null = null;
  #timer: ReturnType<typeof setTimeout> | null = null;
  #backoff = RECONNECT_MIN_MS;
  #closed = false;

  get readOnly(): boolean {
    return this.role === "viewer";
  }

  /** Whether a start would have to wait for the hardware to settle, rounded
   *  the way the status feed rounds it. */
  get hardwareWait(): number {
    return this.session?.hardware_wait_s ?? 0;
  }

  /** The supervisor is mid-transition, so a start or a stop would only come
   *  back as a 409. */
  get busy(): boolean {
    const phase = this.session?.state;
    return phase === "starting" || phase === "stopping";
  }

  /** The performance state of each running system, in ensemble order. Empty
   *  between shows — the bridge answers an idle host with no systems rather
   *  than an error, so the live screen has something honest to render. */
  get systems(): PerfSystem[] {
    return this.frame.systems ?? [];
  }

  connect(): void {
    this.#closed = false;
    this.#open();
  }

  close(): void {
    this.#closed = true;
    if (this.#timer !== null) clearTimeout(this.#timer);
    this.#timer = null;
    this.#socket?.close();
    this.#socket = null;
  }

  /** Send a command frame. The socket is one channel in both directions, so a
   *  screen that already has the feed does not open a second connection to act
   *  on it — but a viewer's frames are dropped server-side, so don't pretend
   *  here that they were sent. */
  send(command: Record<string, unknown>): boolean {
    if (this.readOnly) return false;
    if (this.#socket === null || this.#socket.readyState !== WebSocket.OPEN) return false;
    this.#socket.send(JSON.stringify(command));
    return true;
  }

  #open(): void {
    let socket: WebSocket;
    try {
      socket = new WebSocket(socketUrl());
    } catch {
      this.#scheduleReconnect();
      return;
    }
    this.#socket = socket;

    socket.onopen = () => {
      this.connected = true;
      this.#backoff = RECONNECT_MIN_MS;
    };
    socket.onmessage = (event) => this.#absorb(event.data);
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      this.connected = false;
      this.#socket = null;
      this.#scheduleReconnect();
    };
  }

  #scheduleReconnect(): void {
    if (this.#closed || this.#timer !== null) return;
    const delay = this.#backoff;
    this.#backoff = Math.min(delay * 2, RECONNECT_MAX_MS);
    this.#timer = setTimeout(() => {
      this.#timer = null;
      this.#open();
    }, delay);
  }

  #absorb(raw: unknown): void {
    if (typeof raw !== "string") return;
    let frame: StateFrame;
    try {
      frame = JSON.parse(raw) as StateFrame;
    } catch {
      return;
    }
    this.frame = frame;
    this.ready = true;
    if (frame.role !== undefined) this.role = frame.role;
    if (frame.session) this.session = frame.session;
    if (frame.log && frame.log.length) {
      // Appended by sequence number rather than replaced: the daemon sends
      // only what this connection has not seen, so a re-sent tail would
      // duplicate every line three times a second.
      const merged = this.log.concat(frame.log);
      this.log = merged.length > LOG_LIMIT ? merged.slice(merged.length - LOG_LIMIT) : merged;
    }
  }
}
