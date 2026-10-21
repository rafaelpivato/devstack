import { createRoot } from "react-dom/client";
import { useCallback } from "react";
import { readRuntime } from "./api/runtime";
import { useResource } from "./hooks/useResource";
import { Screen, StateLabel } from "./components/Screen";
import "./styles.css";

/** Staging entry only. The v0.5.3 UI remains the default until I2–I7. */
function App() {
  const load = useCallback(readRuntime, []);
  const runtime = useResource(load);

  return <Screen title="Frontend preview" footer={<a href="/">Open current dashboard</a>}>
    <h1>Frontend foundation</h1>
    <p className="muted">Preview only. The current dashboard is unchanged.</p>
    <section className="panel" aria-live="polite">
      <h2>Controller runtime</h2>
      {runtime.kind === "loading" && <StateLabel text="Checking…" icon="◌" />}
      {runtime.kind === "error" && <StateLabel text="Unavailable" icon="!" />}
      {runtime.kind === "ready" &&
        <StateLabel icon={runtime.data.phase === "active" ? "●" : "○"}
          text={runtime.data.phase} />}
    </section>
  </Screen>;
}

const mount = document.getElementById("app");
if (mount) createRoot(mount).render(<App />);
