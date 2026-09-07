/** The page for an address this application does not have.
 *
 * There was no such page. The backend serves `index.html` for any path it does
 * not recognise — that is how a single-page app has to work — and the router
 * then matched nothing and rendered an empty shell inside the sidebar. A typed
 * URL, a stale bookmark or a renamed route all produced a blank panel with no
 * explanation and no way back except the browser's own controls.
 */

import { Link } from "react-router-dom";
import { Home } from "lucide-react";
import { PulseIcon } from "@/components/BrandIcons";
import { BrandFooter } from "@/components/BrandFooter";

export default function NotFoundPage() {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <div className="flex-1 flex items-center justify-center px-6">
        <div className="text-center">
          <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-primary/10">
            <PulseIcon className="text-primary" size={32} />
          </div>
          <p className="text-5xl font-bold text-text">404</p>
          <h1 className="mt-2 text-xl font-semibold">This page does not exist</h1>
          <p className="mt-2 max-w-md text-sm text-text-muted">
            Spark Pulse has no page at this address. If you followed a link from somewhere
            inside the application, the route it pointed at has been renamed or removed.
          </p>
          <Link
            to="/"
            className="mt-6 inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-primary hover:bg-primary-hover text-white font-medium text-sm transition-colors"
          >
            <Home size={16} />
            Back to Recipes
          </Link>
        </div>
      </div>
      <BrandFooter />
    </div>
  );
}
