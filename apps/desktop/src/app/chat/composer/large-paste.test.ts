import { describe, expect, it } from 'vitest'

import { isGoalComposerContent, LARGE_PASTE_ATTACHMENT_THRESHOLD, pasteSizeLabel, shouldConvertPasteToAttachment } from './large-paste'

describe('large paste policy', () => {
  it('converts only pastes strictly past the threshold', () => {
    expect(shouldConvertPasteToAttachment('a'.repeat(LARGE_PASTE_ATTACHMENT_THRESHOLD))).toBe(false)
    expect(shouldConvertPasteToAttachment('a'.repeat(LARGE_PASTE_ATTACHMENT_THRESHOLD + 1))).toBe(true)
    expect(shouldConvertPasteToAttachment('a'.repeat(50_000), 0)).toBe(false)
  })

  it('labels the chip by encoded byte size, not character count', () => {
    expect(pasteSizeLabel('a'.repeat(512))).toBe('512 B')
    expect(pasteSizeLabel('\u00e9'.repeat(1024))).toBe('2.0 KB')
  })

  describe('isGoalComposerContent', () => {
    it('detects /goal at the start of the content', () => {
      expect(isGoalComposerContent('/goal audit the full stack')).toBe(true)
      expect(isGoalComposerContent('/goal')).toBe(true)
    })

    it('tolerates leading whitespace before the directive', () => {
      expect(isGoalComposerContent('  /goal audit the full stack')).toBe(true)
      expect(isGoalComposerContent('\n/goal audit the full stack')).toBe(true)
    })

    it('requires a command boundary after /goal', () => {
      expect(isGoalComposerContent('/goal2 audit the full stack')).toBe(false)
      expect(isGoalComposerContent('/goals audit the full stack')).toBe(false)
      expect(isGoalComposerContent('/goalkeeping notes')).toBe(false)
    })

    it('only counts /goal at the start of the content', () => {
      expect(isGoalComposerContent('texto previo /goal audit the full stack')).toBe(false)
      expect(isGoalComposerContent('verifica /goal luego')).toBe(false)
    })

    it('rejects empty and non-goal content', () => {
      expect(isGoalComposerContent('')).toBe(false)
      expect(isGoalComposerContent('   ')).toBe(false)
      expect(isGoalComposerContent('normal message')).toBe(false)
      expect(isGoalComposerContent('/other directive')).toBe(false)
    })
  })
})
