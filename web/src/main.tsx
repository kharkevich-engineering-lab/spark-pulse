import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { initTheme } from "./lib/theme";

// The inline script in index.html paints the stored theme; this is what makes
// "System" keep following the OS after load, by subscribing to the media query.
initTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
