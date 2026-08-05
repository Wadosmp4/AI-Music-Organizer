import { useState } from "react";

import { Onboarding } from "./pages/Onboarding";
import { ReviewQueue } from "./pages/ReviewQueue";
import { Settings } from "./pages/Settings";

type Page = "review" | "settings" | "onboarding";

export function App() {
  const [page, setPage] = useState<Page>("review");

  return (
    <div className="app">
      <nav>
        <button onClick={() => setPage("review")}>Review Queue</button>
        <button onClick={() => setPage("onboarding")}>Onboarding</button>
        <button onClick={() => setPage("settings")}>Settings</button>
      </nav>
      {page === "review" && <ReviewQueue />}
      {page === "settings" && <Settings />}
      {page === "onboarding" && <Onboarding onComplete={() => setPage("review")} />}
    </div>
  );
}
