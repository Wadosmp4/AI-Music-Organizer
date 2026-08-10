import { useEffect, useRef, useState } from "react";

import { fetchReviewQueue } from "./api/client";
import { Layout, type Page } from "./components/Layout";
import { Onboarding } from "./pages/Onboarding";
import { ReviewQueue } from "./pages/ReviewQueue";
import { Settings } from "./pages/Settings";

export function App() {
  // R16: Playlists is the default landing page -- overridden below, once,
  // if the mount check below finds pending Organize work.
  const [page, setPage] = useState<Page>("playlists");
  // Guards the one-time mount redirect below from clobbering a page change
  // the user already made (e.g. clicking a nav item) while that check was
  // still in flight.
  const navigatedRef = useRef(false);

  function handlePageChange(next: Page) {
    navigatedRef.current = true;
    setPage(next);
  }

  useEffect(() => {
    // R16/KTD7: reuses the exact same review-queue fetch ReviewQueue already
    // makes on its own mount, rather than a dedicated "has pending work"
    // endpoint -- if there's any pending item, open straight to Organize
    // instead of making the user visit Playlists first.
    void fetchReviewQueue()
      .then((items) => {
        if (navigatedRef.current) return;
        const hasPendingItems = items.some((item) => item.status === "pending");
        if (hasPendingItems) setPage("organize");
      })
      .catch(() => {
        // No pending-work signal available -- fall back to the default
        // Playlists landing rather than blocking on it.
      });
  }, []);

  return (
    <Layout page={page} onPageChange={handlePageChange}>
      {page === "organize" && <ReviewQueue />}
      {page === "settings" && <Settings />}
      {page === "playlists" && <Onboarding onComplete={() => setPage("organize")} />}
    </Layout>
  );
}
