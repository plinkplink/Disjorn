/* What a device owes an account that signs out of it. Pure: the caller hands
   in the browser's subscription and the server call. */

export interface DeviceSubscription {
  endpoint: string;
  unsubscribe: () => Promise<boolean>;
}

/** The server is asked first, while the session can still ask it. The
    unsubscribe then kills the endpoint itself, which is what ends delivery
    when the server refused or never heard. Never rejects. */
export async function releaseDevice(
  sub: DeviceSubscription | null,
  forget: (endpoint: string) => Promise<unknown>,
): Promise<boolean> {
  if (sub === null) return false;
  try {
    await forget(sub.endpoint);
  } catch {
    /* the unsubscribe below still ends delivery */
  }
  try {
    return await sub.unsubscribe();
  } catch {
    return false;
  }
}
