/** Global refresh registry — pages register their refetch function here. */

let refreshFn: (() => void) | null = null;

export function setRefresh(fn: () => void) {
  refreshFn = fn;
}

export function doRefresh() {
  refreshFn?.();
}
