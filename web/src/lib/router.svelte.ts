/**
 * Which screen is showing, kept in the address bar.
 *
 * Small on purpose — two screens and a tail — but real paths rather than a
 * tab variable, because the server already answers any unclaimed path with the
 * app shell (`c64cast/control/web_static.py`). That means `/config/shows/gig.toml`
 * is a link somebody can send, a bookmark, and a page reload that comes back
 * where it was; the back button works because it is the browser's own history
 * rather than something reimplemented on top of it.
 */

export type Screen = "session" | "config" | "live";

interface Route {
  screen: Screen;
  /** Whatever followed the screen's own segment — a config ref for `config`, a
   *  system name for `live`. */
  tail: string;
}

/** The screens that own a path segment. `session` is the root and so has none. */
const NAMED: readonly Screen[] = ["config", "live"];

function parse(pathname: string): Route {
  const parts = pathname.split("/").filter((p) => p !== "");
  const head = parts[0] as Screen;
  if (NAMED.includes(head)) {
    return { screen: head, tail: parts.slice(1).map(decodeURIComponent).join("/") };
  }
  return { screen: "session", tail: "" };
}

export class Router {
  #route = $state<Route>(parse(window.location.pathname));
  #onPop = () => {
    this.#route = parse(window.location.pathname);
  };

  constructor() {
    window.addEventListener("popstate", this.#onPop);
  }

  dispose(): void {
    window.removeEventListener("popstate", this.#onPop);
  }

  get screen(): Screen {
    return this.#route.screen;
  }

  get tail(): string {
    return this.#route.tail;
  }

  go(screen: Screen, tail = ""): void {
    const path = screen === "session" ? "/" : `/${screen}${tail ? `/${encode(tail)}` : ""}`;
    if (path === window.location.pathname) return;
    // `pushState` rather than `replaceState`: choosing a different config is a
    // step the reader may want to walk back out of.
    window.history.pushState({}, "", path);
    this.#route = parse(window.location.pathname);
  }
}

function encode(tail: string): string {
  return tail
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
}
