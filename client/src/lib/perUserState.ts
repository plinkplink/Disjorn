/* One account's values that live at module level, outside any store's state.
   The module that keeps one says here how to forget it; stores/perUser.ts runs
   them all when the account changes. */

interface Registered {
  names: string[];
  forget: () => void;
}

const registered: Registered[] = [];

/** `module` is the file's path under src without its extension; the reset
    test matches these names against the declarations in the source. */
export function perUserValues(
  module: string,
  identifiers: string[],
  forget: () => void,
): void {
  registered.push({ names: identifiers.map((id) => `${module}:${id}`), forget });
}

export function perUserValueNames(): string[] {
  return registered.flatMap((r) => r.names);
}

export function forgetPerUserValues(): void {
  for (const r of registered) r.forget();
}
