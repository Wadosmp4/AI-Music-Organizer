import { useState } from "react";

import { Layout, type Page } from "./components/Layout";
import { Onboarding } from "./pages/Onboarding";
import { ReviewQueue } from "./pages/ReviewQueue";
import { Settings } from "./pages/Settings";

export function App() {
  const [page, setPage] = useState<Page>("review");

  return (
    <Layout page={page} onPageChange={setPage}>
      {page === "review" && <ReviewQueue />}
      {page === "settings" && <Settings />}
      {page === "onboarding" && <Onboarding onComplete={() => setPage("review")} />}
    </Layout>
  );
}
