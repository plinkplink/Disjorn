import { useEffect } from "react";

import { UpdateToast } from "./components/UpdateToast";
import { useSession } from "./stores/session";
import { AppShell } from "./views/AppShell";
import { ChangePasswordPage } from "./views/ChangePasswordPage";
import { LoginPage } from "./views/LoginPage";
import { UnreachablePage } from "./views/UnreachablePage";

export function App() {
  const user = useSession((s) => s.user);
  const booting = useSession((s) => s.booting);
  const bootUnreachable = useSession((s) => s.bootUnreachable);
  const bootstrap = useSession((s) => s.bootstrap);
  const mustChangePassword = useSession((s) => s.mustChangePassword);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  if (booting) return <div className="boot-splash">Loading…</div>;

  // Four states, in order of authority: unreachable, signed out, walled off,
  // in.
  //
  // Unreachable comes FIRST because it is the one state where `user === null`
  // says nothing about the session — the server was never asked. Falling
  // through to the login form there is the bug this ordering fixes.
  //
  // The rotation wall sits between the other two deliberately. While the flag
  // is set the server 403s every route but three, so there is no shell to
  // render behind a modal — the shell would mount, fire its channel fetches,
  // and paint an app made entirely of failed requests.
  let screen;
  if (bootUnreachable) screen = <UnreachablePage />;
  else if (user === null) screen = <LoginPage />;
  else if (mustChangePassword) screen = <ChangePasswordPage />;
  else screen = <AppShell />;

  return (
    <>
      {screen}
      <UpdateToast />
    </>
  );
}
