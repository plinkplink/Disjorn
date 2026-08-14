import { useEffect } from "react";

import { UpdateToast } from "./components/UpdateToast";
import { useSession } from "./stores/session";
import { AppShell } from "./views/AppShell";
import { ChangePasswordPage } from "./views/ChangePasswordPage";
import { LoginPage } from "./views/LoginPage";

export function App() {
  const user = useSession((s) => s.user);
  const booting = useSession((s) => s.booting);
  const bootstrap = useSession((s) => s.bootstrap);
  const mustChangePassword = useSession((s) => s.mustChangePassword);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  if (booting) return <div className="boot-splash">Loading…</div>;

  // Three states, in order of authority: signed out, walled off, in.
  //
  // The rotation wall sits between the other two deliberately. While the flag
  // is set the server 403s every route but three, so there is no shell to
  // render behind a modal — the shell would mount, fire its channel fetches,
  // and paint an app made entirely of failed requests.
  let screen;
  if (user === null) screen = <LoginPage />;
  else if (mustChangePassword) screen = <ChangePasswordPage />;
  else screen = <AppShell />;

  return (
    <>
      {screen}
      <UpdateToast />
    </>
  );
}
