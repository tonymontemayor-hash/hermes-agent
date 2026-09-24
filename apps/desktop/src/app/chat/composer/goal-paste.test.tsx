// @vitest-environment jsdom
/**
 * `/goal` must never be collapsed into a `.txt` attachment on a large paste.
 *
 * The large-paste policy (large-paste.ts) converts a >3k-char plain-text
 * paste into a `Pasted content` file chip. A `/goal` directive must NOT be
 * converted: the goal text is the payload and has to reach `prompt.submit`
 * inline, in full. This exercises the real `ChatBar.handlePaste` wiring
 * (render → fire a native `paste` ClipboardEvent → assert) over the six
 * mandatory cases:
 *
 *   1) empty composer     + paste `/goal <big>`   → inline (no attach)
 *   2) composer `/goal `  + paste `<big>`         → inline (no attach)
 *   3) composer `/goal\n` + paste `<big>`         → inline (no attach)
 *   4) composer `normal`  + paste `<big>`         → attachment (unchanged)
 *   5) empty composer     + paste `/goal2 <big>`  → attachment (not /goal)
 *   6) composer `prior`   + paste `/goal <big>`   → attachment (preceding text)
 */
import { cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChatBar } from './index'
import { I18nProvider } from '@/i18n'
import { ThreadRuntime } from '@/components/assistant-ui/test-utils'
import { composerPlainText, placeCaretEnd } from './rich-editor'
import { clearSessionDraft } from '@/store/composer'
import type { ChatBarProps, ChatBarState } from './types'

/** The onAttachPastedText contract, narrowed so the vi.fn mock typechecks. */
type OnAttachPastedText = NonNullable<ChatBarProps['onAttachPastedText']>

/** Minimal but complete ChatBarState — tools disabled (no menu), voice off. */
const chatBarState: ChatBarState = {
  model: { model: 'test-model', provider: 'test', canSwitch: false },
  tools: { enabled: false, label: 'Tools' },
  voice: { enabled: false, active: false }
}

/** Unique session id per render so each test gets its own draft scope. */
let sessionSeq = 0

function renderComposer({
  onAttachPastedText = vi.fn<OnAttachPastedText>(),
  composerText = ''
}: {
  onAttachPastedText?: ReturnType<typeof vi.fn<OnAttachPastedText>>
  composerText?: string
}) {
  const sessionId = `goal-paste-test-${++sessionSeq}`
  const utils = render(
    <ThreadRuntime messages={[]}>
      <MemoryRouter>
        <I18nProvider configClient={null} initialLocale="en">
          <ChatBar
            busy={false}
            disabled={false}
            sessionId={sessionId}
            state={chatBarState}
            onSubmit={async () => false}
            onCancel={() => {}}
            onAttachPastedText={onAttachPastedText}
          />
        </I18nProvider>
      </MemoryRouter>
    </ThreadRuntime>
  )

  const editor = utils.container.querySelector<HTMLDivElement>('[contenteditable="true"]')

  if (!editor) {
    throw new Error('contenteditable editor not found — ChatBar mount changed?')
  }

  if (composerText) {
    // The editor is uncontrolled (DOM is the source of truth); pre-fill the
    // pre-paste state by writing escaped plain text. No slash/URL refs → no chips.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    editor.innerHTML = composerText.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') as any
  }

  // jsdom has no real selection, so a bare paste would insert at the start of
  // the editor. Put the caret at the end so the paste lands where the user's
  // caret actually sits (after any pre-typed `/goal ` prefix).
  placeCaretEnd(editor)

  return { editor, onAttachPastedText }
}

function firePaste(target: HTMLElement, text: string): void {
  // jsdom has no `DataTransfer` and its `ClipboardEvent` constructor ignores
  // `clipboardData`. The handler only reads `getData('text')` and
  // `extractClipboardImageBlobs` (which scans `items`/`files`). Both are
  // satisfied by a minimal stub with empty item/file lists; we attach it via
  // `defineProperty` because `clipboardData` is read-only on real events.
  const clipboardData = {
    items: [],
    files: [],
    types: ['text/plain'],
    getData: () => text
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any

  const event = new Event('paste', { bubbles: true, cancelable: true })

  Object.defineProperty(event, 'clipboardData', { value: clipboardData })
  target.dispatchEvent(event)
}

const bigPaste = `goal-arg ${'lorem ipsum dolor sit amet '.repeat(250)}` // ~4500 chars

afterEach(async () => {
  cleanup()
  // Clear every draft scope this file created so no /goal text leaks into the
  // next test's editor (the composer store is module-level + localStorage).
  for (let i = 1; i <= sessionSeq; i += 1) {
    clearSessionDraft(`goal-paste-test-${i}`)
  }
  clearSessionDraft(null)
  clearSessionDraft('__new__')
})

describe('/goal large-paste stays inline (mandatory matrix)', () => {
  it('1) empty composer + paste "/goal <big>" → stays inline', () => {
    const { editor, onAttachPastedText } = renderComposer({ composerText: '' })
    firePaste(editor, `/goal ${bigPaste}`)

    expect(onAttachPastedText).not.toHaveBeenCalled()
    // Full goal text landed in the editor (inline), not a .txt chip. The
    // slash body may serialize as a chip (refText) or plain text, so check the
    // editor carries the directive + a substantial run of the body.
    const plain = composerPlainText(editor)

    expect(plain).toContain('/goal')
    expect(plain).toContain('goal-arg')
    expect(plain.length).toBeGreaterThan(3000)
  })

  it('2) composer="/goal " + paste <big> → stays inline', () => {
    const { editor, onAttachPastedText } = renderComposer({ composerText: '/goal ' })
    firePaste(editor, bigPaste)

    expect(onAttachPastedText).not.toHaveBeenCalled()
    const plain = composerPlainText(editor)

    expect(plain).toContain('/goal')
    expect(plain).toContain('goal-arg')
    expect(plain.length).toBeGreaterThan(3000)
  })

  it('3) composer="/goal\\n" + paste <big> → stays inline', () => {
    const { editor, onAttachPastedText } = renderComposer({ composerText: '/goal\n' })
    firePaste(editor, bigPaste)

    expect(onAttachPastedText).not.toHaveBeenCalled()
    const plain = composerPlainText(editor)

    expect(plain).toContain('/goal')
    expect(plain).toContain('goal-arg')
    expect(plain.length).toBeGreaterThan(3000)
  })

  it('4) composer="normal" + paste <big> → attachment (unchanged behavior)', () => {
    const { editor, onAttachPastedText } = renderComposer({ composerText: 'normal' })
    firePaste(editor, bigPaste)

    expect(onAttachPastedText).toHaveBeenCalledTimes(1)
    // The payload rode along as a file; the pre-existing text is untouched.
    expect(composerPlainText(editor)).toBe('normal')
  })

  it('5) empty composer + paste "/goal2 <big>" → attachment (not /goal)', () => {
    const { editor, onAttachPastedText } = renderComposer({ composerText: '' })
    const paste = `/goal2 ${bigPaste}`

    firePaste(editor, paste)

    expect(onAttachPastedText).toHaveBeenCalledTimes(1)
    // Editor stays empty (the whole paste became the attachment).
    expect(composerPlainText(editor).trim()).toBe('')
  })

  it('6) composer="texto previo" + paste "/goal <big>" → attachment', () => {
    const { editor, onAttachPastedText } = renderComposer({ composerText: 'texto previo' })
    const paste = `/goal ${bigPaste}`

    firePaste(editor, paste)

    expect(onAttachPastedText).toHaveBeenCalledTimes(1)
    // Preceding text is preserved; the paste did NOT inline-merge as /goal.
    expect(composerPlainText(editor)).toBe('texto previo')
  })
})
