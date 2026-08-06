// KTD9: GetSongBPM's terms require an attribution credit anywhere its
// measured tempo data is shown. Rendered by any page/item that has
// bpm_source === "measured" — never for an LLM-estimated bpm.
export function BpmAttribution() {
  return (
    <p
      data-testid="bpm-attribution"
      className="inline-flex w-fit items-center gap-1 rounded-full bg-slate-100 px-2.5 py-0.5 text-xs text-slate-500"
    >
      Tempo data powered by{" "}
      <a
        href="https://getsongbpm.com"
        target="_blank"
        rel="noreferrer"
        className="font-medium text-accent hover:underline"
      >
        GetSongBPM
      </a>
    </p>
  );
}
