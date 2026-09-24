/**
 * Large-paste-to-attachment policy.
 *
 * Pasting more than ~3k characters converts the content into a text
 * attachment instead of inserting it inline, keeping the composer clean and
 * preventing a single paste from flooding the input. Short pastes stay inline;
 * the threshold lives here so every paste handler shares one policy.
 */

/** Characters beyond which a plain-text paste becomes a `.txt` attachment. */
export const LARGE_PASTE_ATTACHMENT_THRESHOLD = 3_000

/**
 * True when a plain-text paste should be converted into a text attachment
 * rather than inserted inline. Only sheer size qualifies — rich clipboard
 * data, images, and files never route through this path (they have their own
 * pipelines upstream of this check).
 */
export function shouldConvertPasteToAttachment(
  text: string,
  threshold: number = LARGE_PASTE_ATTACHMENT_THRESHOLD
): boolean {
  return typeof text === 'string' && threshold > 0 && text.length > threshold
}

/**
 * True when the composer content is a `/goal` directive: the text starts
 * with `/goal` at a command boundary (space, tab or end of string).
 * `/goal` pastes must stay inline so the full goal argument reaches the
 * backend; a `.txt` attachment would replace the inline payload with an
 * `@file:` ref and the command would arrive detached from its text.
 */
export function isGoalComposerContent(content: string): boolean {
  const text = content.trimStart()

  return /^\/goal(?=\s|$)/.test(text)
}

/**
 * True when a large plain-text paste must stay INLINE rather than being
 * converted to a `.txt` attachment because it is a `/goal` directive.
 *
 * `composerText` is the editor's serialized content BEFORE this paste lands
 * (see `composerPlainText`), `pastedText` is the sanitized clipboard text.
 * Two workflows qualify:
 *   (a) the user already typed `/goal ` (composer holds the directive) and
 *       pastes the big body — the whole message is the goal;
 *   (b) the whole `/goal <body>` block is pasted at once into an empty
 *       composer (empty editor + `/goal`-prefixed paste).
 * Replacing a selection in a non-empty composer is deliberately NOT exempt,
 * and only `/goal` is exempt — no other slash command is generalized yet.
 */
export function isGoalInlinePaste(composerText: string, pastedText: string): boolean {
  return isGoalComposerContent(composerText) || (!composerText.trim() && isGoalComposerContent(pastedText))
}

/** Human-readable size of a paste's UTF-8 bytes, for the attachment chip. */
export function pasteSizeLabel(text: string): string {
  const bytes = new TextEncoder().encode(text).length

  if (bytes < 1024) {
    return `${bytes} B`
  }

  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }

  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
