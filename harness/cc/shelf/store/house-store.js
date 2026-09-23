'use strict';
/* house-store.js — the Disjorn house store, v1 (spec §G).
 *
 * SOURCE OF TRUTH. `house-store.mjs` is generated from this file by build.mjs
 * and differs only in the tail below; edit this file, run `node build.mjs`,
 * commit both.
 *
 *     <script src="vendor/house-store.js"></script>   ->  window.houseStore
 *     import { houseStore } from './vendor/house-store.mjs';
 *
 *     const store = houseStore('abcdefghijkm');   // your app id
 *     store.set('user', 'score', 12);
 *     store.get('user', 'score');                 // 12
 *     store.keys('user');                         // ['score']
 *     store.del('user', 'score');
 *
 * Scopes: 'user' is this browser's localStorage, namespaced
 * `disjorn:<appId>:user:<key>`, values JSON-encoded. 'app' — storage shared by
 * everyone who uses the app — throws StoreScopeNotAvailable until the server
 * side of it exists. Anything else throws RangeError.
 *
 * No dependencies, no build step, no network. It is a namespacing and
 * JSON-encoding convention with the edges guarded, and that is all it is.
 */

const STORE_VERSION = 1;

class StoreScopeNotAvailable extends Error {
  constructor(message) {
    super(message);
    this.name = 'StoreScopeNotAvailable';
  }
}

const houseStore = (() => {
  const APP_ID = /^[a-z2-7]{12}$/;

  // Every scope decision happens here, before any storage is touched, so
  // `store.get('app', k)` throws the scope error even where localStorage is
  // unavailable.
  function prefixFor(appId, scope) {
    if (scope === 'user') return 'disjorn:' + appId + ':user:';
    if (scope === 'app') {
      throw new StoreScopeNotAvailable('app scope arrives with server-side storage');
    }
    throw new RangeError('unknown store scope ' + JSON.stringify(scope) + ', expected "user" or "app"');
  }

  function fullKey(appId, scope, key) {
    const prefix = prefixFor(appId, scope);
    if (typeof key !== 'string' || key === '') {
      throw new RangeError('store key must be a non-empty string');
    }
    return prefix + key;
  }

  function backing() {
    const ls = globalThis.localStorage;
    if (!ls) throw new Error('house store: localStorage is not available here');
    return ls;
  }

  return function houseStore(appId) {
    if (typeof appId !== 'string' || !APP_ID.test(appId)) {
      throw new RangeError('app id must match ^[a-z2-7]{12}$, got ' + JSON.stringify(appId));
    }
    return {
      get(scope, key) {
        const k = fullKey(appId, scope, key);
        const raw = backing().getItem(k);
        return raw === null || raw === undefined ? undefined : JSON.parse(raw);
      },
      set(scope, key, value) {
        const k = fullKey(appId, scope, key);
        const raw = JSON.stringify(value);
        if (raw === undefined) {
          throw new TypeError('store values must be JSON-serialisable; undefined is not');
        }
        backing().setItem(k, raw);
        return value;
      },
      del(scope, key) {
        backing().removeItem(fullKey(appId, scope, key));
      },
      keys(scope) {
        const prefix = prefixFor(appId, scope);
        const ls = backing();
        const found = [];
        for (let i = 0; i < ls.length; i++) {
          const k = ls.key(i);
          if (typeof k === 'string' && k.startsWith(prefix)) found.push(k.slice(prefix.length));
        }
        return found.sort();
      },
    };
  };
})();

houseStore.STORE_VERSION = STORE_VERSION;
houseStore.StoreScopeNotAvailable = StoreScopeNotAvailable;

/* --- BUILD TAIL: the classic and ESM builds differ here and nowhere else. --- */
window.houseStore = houseStore;
window.STORE_VERSION = STORE_VERSION;
window.StoreScopeNotAvailable = StoreScopeNotAvailable;
