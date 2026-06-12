// Hostile-page event guard.
//
// Sites like Google Sheets/Docs register document-level capture
// listeners that ACT on keyboard / clipboard / mouse events globally:
// they draw their own context menu on right-click, run their own paste
// handler, re-grab focus to the grid editor, and preventDefault()
// shortcuts. Events originating inside our widget cross that document
// level on the capture path (window → document → … → shadow host →
// shadow root → target), so the page both swallows AND reacts to a
// Cmd+V or right-click aimed at one of our inputs.
//
// Merely defusing preventDefault isn't enough — the page must not SEE
// widget-origin events at all. So:
//
//   1. window capture (runs BEFORE any document-level listener): an
//      event whose retargeted target is our shadow host gets
//      stopImmediatePropagation() — no page listener ever observes it.
//      We do NOT preventDefault, so browser defaults survive: text
//      insertion, native context menu, focus-on-mousedown, selection.
//   2. Because stopping at window also starves React's listeners
//      inside the shadow root, we synchronously dispatch a CLONE of
//      the event on the real inner target (shadow.activeElement for
//      key/clipboard, elementFromPoint for mouse). The clone is
//      composed: false, so it propagates only within the shadow tree —
//      the page can never observe it either. React handlers run on the
//      clone exactly as they would have on the original.
//   3. If a widget handler preventDefault()s the clone (Enter-to-send,
//      paste-an-image), we mirror that onto the original event — its
//      default action is decided after our window listener returns.
//
// The closed shadow root makes the origin test exact: outside
// listeners see events retargeted to the host element, so
// `e.target === host` is precisely "this event came from inside".
//
// Known limit: a page listener registered on *window* capture before
// the bookmarklet loads still runs ahead of us. The hijackers seen in
// the wild (Sheets, Docs, Notion) sit on document, so this stays
// theoretical.

const KEY_TYPES = ["keydown", "keypress", "keyup"] as const;
const CLIPBOARD_TYPES = ["copy", "cut", "paste"] as const;
const INPUT_TYPES = ["beforeinput"] as const;
const MOUSE_TYPES = ["mousedown", "mouseup", "dblclick", "contextmenu"] as const;
const DRAG_TYPES = ["dragenter", "dragover", "dragleave", "drop"] as const;
const PLAIN_TYPES = ["selectstart"] as const;

function cloneKeyboard(e: KeyboardEvent): KeyboardEvent {
  return new KeyboardEvent(e.type, {
    key: e.key,
    code: e.code,
    location: e.location,
    ctrlKey: e.ctrlKey,
    shiftKey: e.shiftKey,
    altKey: e.altKey,
    metaKey: e.metaKey,
    repeat: e.repeat,
    isComposing: e.isComposing,
    bubbles: true,
    cancelable: true,
    composed: false,
  });
}

function cloneClipboard(e: ClipboardEvent): ClipboardEvent {
  const clone = new ClipboardEvent(e.type, {
    bubbles: true,
    cancelable: true,
    composed: false,
  });
  // ClipboardEventInit.clipboardData is ignored by some engines —
  // forward the live DataTransfer via a getter instead.
  Object.defineProperty(clone, "clipboardData", {
    get: () => e.clipboardData,
  });
  return clone;
}

function cloneInput(e: InputEvent): InputEvent {
  const clone = new InputEvent(e.type, {
    inputType: e.inputType,
    data: e.data,
    isComposing: e.isComposing,
    bubbles: true,
    cancelable: e.cancelable,
    composed: false,
  });
  Object.defineProperty(clone, "dataTransfer", {
    get: () => e.dataTransfer,
  });
  return clone;
}

function cloneMouse(e: MouseEvent): MouseEvent {
  return new MouseEvent(e.type, {
    button: e.button,
    buttons: e.buttons,
    clientX: e.clientX,
    clientY: e.clientY,
    screenX: e.screenX,
    screenY: e.screenY,
    ctrlKey: e.ctrlKey,
    shiftKey: e.shiftKey,
    altKey: e.altKey,
    metaKey: e.metaKey,
    detail: e.detail,
    bubbles: true,
    cancelable: true,
    composed: false,
  });
}

function cloneDrag(e: DragEvent): DragEvent {
  const clone = new DragEvent(e.type, {
    button: e.button,
    buttons: e.buttons,
    clientX: e.clientX,
    clientY: e.clientY,
    screenX: e.screenX,
    screenY: e.screenY,
    ctrlKey: e.ctrlKey,
    shiftKey: e.shiftKey,
    altKey: e.altKey,
    metaKey: e.metaKey,
    bubbles: true,
    cancelable: true,
    composed: false,
  });
  Object.defineProperty(clone, "dataTransfer", {
    get: () => e.dataTransfer,
  });
  return clone;
}

function clonePlain(e: Event): Event {
  return new Event(e.type, {
    bubbles: true,
    cancelable: true,
    composed: false,
  });
}

export function installEventGuard(host: HTMLElement, shadow: ShadowRoot): void {
  const guard = (
    e: Event,
    makeClone: (e: Event) => Event,
    innerTarget: (e: Event) => Element | null,
  ) => {
    if (!e.isTrusted) return; // never re-guard our own clones
    if (e.target !== host) return; // not from inside the widget
    // Hide from the page entirely — document-level hijackers (Sheets'
    // context menu, paste-into-grid, focus stealers) never run.
    e.stopImmediatePropagation();
    const target = innerTarget(e);
    if (!target) return; // default action still proceeds
    const clone = makeClone(e);
    target.dispatchEvent(clone); // synchronous; React handlers run here
    if (clone.defaultPrevented) e.preventDefault();
  };

  const focusTarget = () => shadow.activeElement;
  const pointTarget = (e: Event) => {
    const m = e as MouseEvent;
    return (
      shadow.elementFromPoint(m.clientX, m.clientY) ?? shadow.activeElement
    );
  };

  for (const t of [...KEY_TYPES])
    window.addEventListener(
      t,
      (e) => guard(e, (x) => cloneKeyboard(x as KeyboardEvent), focusTarget),
      true,
    );
  for (const t of [...CLIPBOARD_TYPES])
    window.addEventListener(
      t,
      (e) => guard(e, (x) => cloneClipboard(x as ClipboardEvent), focusTarget),
      true,
    );
  for (const t of [...INPUT_TYPES])
    window.addEventListener(
      t,
      (e) => guard(e, (x) => cloneInput(x as InputEvent), focusTarget),
      true,
    );
  for (const t of [...MOUSE_TYPES])
    window.addEventListener(
      t,
      (e) => guard(e, (x) => cloneMouse(x as MouseEvent), pointTarget),
      true,
    );
  for (const t of [...DRAG_TYPES])
    window.addEventListener(
      t,
      (e) => guard(e, (x) => cloneDrag(x as DragEvent), pointTarget),
      true,
    );
  for (const t of [...PLAIN_TYPES])
    window.addEventListener(t, (e) => guard(e, clonePlain, pointTarget), true);
}
