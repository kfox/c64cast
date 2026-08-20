/** Coalesce a burst of calls into one, firing `ms` after the last one — a
 *  search-as-you-type field's own pace, not the network's. Only the final
 *  call's arguments reach `fn`; every earlier one in the burst is dropped
 *  before it fires. */
export function debounce<A extends unknown[]>(
  fn: (...args: A) => void,
  ms: number,
): (...args: A) => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return (...args: A) => {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn(...args);
    }, ms);
  };
}
