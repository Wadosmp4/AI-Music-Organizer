// KTD9: GetSongBPM's terms require an attribution credit anywhere its
// measured tempo data is shown. Rendered by any page/item that has
// bpm_source === "measured" — never for an LLM-estimated bpm.
export function BpmAttribution() {
  return (
    <p className="bpm-attribution" data-testid="bpm-attribution">
      Tempo data powered by{" "}
      <a href="https://getsongbpm.com" target="_blank" rel="noreferrer">
        GetSongBPM
      </a>
    </p>
  );
}
