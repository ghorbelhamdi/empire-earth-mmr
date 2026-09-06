(function (root, factory) {
  const helpers = factory();
  if (typeof module === 'object' && module.exports) module.exports = helpers;
  else root.companionUI = helpers;
})(globalThis, function () {
  function watchState(state, actionBusy = false) {
    const draft = state.draft;
    if (state.watching) return { disabled: false, label: '■ Stop watching', hint: 'Watching is active. Return to Empire Earth and keep the Military screen visible.', startNew: false };
    if (actionBusy || state.busy) return { disabled: true, label: '◉ Watch for game', hint: 'Wait for the current action to finish before starting the watch.', startNew: false };
    if (state.locked && !draft.result) return { disabled: true, label: '◉ Watch next game', hint: 'Your last submission needs a retry. Use Retry saved submission below before starting another match.', startNew: false };
    if (draft.rows.length || draft.evidence || draft.result) return { disabled: false, label: '◉ Watch next game', hint: draft.result ? 'Your submitted report is saved. Watch next game archives it locally and starts a new draft.' : 'A draft is open. Watch next game asks before archiving it and starting a new draft.', startNew: true };
    return { disabled: false, label: '◉ Watch for game', hint: 'Capture works before you connect to the ladder. Keep the game window visible.', startNew: false };
  }
  function connectionState(state, busy = false, failed = false) {
    const unreadable = Boolean(state.hasSavedToken && !state.hasToken);
    return {
      status: busy ? 'Connecting…' : unreadable ? 'Saved token needs replacement' : failed ? (state.hasToken ? 'Token saved · connection failed' : 'Connection failed') : state.players.length ? `${state.players.length} players connected` : state.hasToken ? 'Token saved on this PC' : 'Setup required',
      tokenNote: unreadable ? 'A token is saved on this PC, but Windows could not unlock it. Paste a replacement or choose Forget token.' : state.hasToken ? 'Token saved securely on this PC. Leave the field empty to keep it, or paste a replacement.' : 'No token saved yet. Paste a device token, then choose Save & connect.',
      tokenPlaceholder: unreadable ? 'Saved token unavailable · paste a replacement' : state.hasToken ? 'Token saved · paste only to replace' : 'Paste the token from your administrator'
    };
  }
  return { watchState, connectionState };
});
