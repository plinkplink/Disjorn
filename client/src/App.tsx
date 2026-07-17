import { useEffect } from "react";

import { useSession } from "./stores/session";
import { AppShell } from "./views/AppShell";
import { LoginPage } from "./views/LoginPage";

export function App() {
  const user = useSession((s) => s.user);
  const booting = useSession((s) => s.booting);
  const bootstrap = useSession((s) => s.bootstrap);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  if (booting) return <div className="boot-splash">Loading…</div>;
  return user === null ? <LoginPage /> : <AppShell />;
}
