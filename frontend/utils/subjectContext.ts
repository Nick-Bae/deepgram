type SubjectRole = 'first' | 'second' | 'third_masc' | 'third_fem' | 'third_plural'

export type SubjectContext = {
  role: SubjectRole
  hint?: string
  updatedAt: number
}

export const SUBJECT_HINTS = {
  third_masc: '그는 ',
  third_fem: '그녀는 ',
  third_plural: '그들은 ',
} as const

export const SUBJECT_HINT_TTL_MS = 10000

const HANGUL_CHAR_RE = /[\uac00-\ud7a3]/

const FIRST_PERSON_MARKERS: readonly string[] = [
  '나는', '난', '내가', '내게', '내게서', '나를', '나만', '나와', '나도', '나에게', '나한테', '나의',
  '저는', '전', '제가', '제게', '저를', '저도', '저만', '저와', '저에게', '저한테', '저의',
  '저희', '저흰', '저희가', '저희는', '저희를', '저희와', '저희에게', '저희의',
  '우리는', '우린', '우리가', '우릴', '우리의', '우리도', '우리만', '우리와', '우리에게', '우리한테', '우리에겐', '우리가', '우리집', '우리나라', '우리교회', '우리'
]

const SECOND_PERSON_MARKERS: readonly string[] = [
  '너는', '넌', '네가', '네게', '너를', '너만', '너와', '너도', '너에게', '너한테',
  '너희', '너희는', '너희가', '너희를', '너희와', '너희에게',
  '당신은', '당신이', '당신을', '당신만', '당신과', '당신께', '당신께서', '당신에게', '당신들', '당신들이', '당신들을', '당신들과', '당신들에게',
  '자네는', '자네가', '자네를', '자네와'
]

const THIRD_MASC_MARKERS: readonly string[] = [
  '그는', '그가', '그를', '그도', '그와', '그에게', '그한테', '그께', '그께서', '그의',
  '그분은', '그분이', '그분께서', '그분을', '그분과', '그분께', '그분에게'
]

const THIRD_FEM_MARKERS: readonly string[] = [
  '그녀는', '그녀가', '그녀를', '그녀도', '그녀와', '그녀에게', '그녀의'
]

const THIRD_PLURAL_MARKERS: readonly string[] = [
  '그들은', '그들이', '그들을', '그들과', '그들에게', '그들의'
]

const ENGLISH_SUBJECT_RULES: readonly { role: SubjectRole; hint?: string; patterns: RegExp[] }[] = [
  {
    role: 'first',
    patterns: [
      /\bI\b/, /\bI['’]m\b/i, /\bI['’]ve\b/i, /\bI['’]d\b/i,
      /\bme\b/i, /\bmy\b/i, /\bmine\b/i,
      /\bwe\b/i, /\bwe['’]re\b/i, /\bwe['’]ve\b/i,
      /\bus\b/i, /\bour\b/i,
    ],
  },
  {
    role: 'third_masc',
    hint: SUBJECT_HINTS.third_masc,
    patterns: [/\bhe\b/i, /\bhe['’]s\b/i, /\bhis\b/i, /\bhim\b/i],
  },
  {
    role: 'third_fem',
    hint: SUBJECT_HINTS.third_fem,
    patterns: [/\bshe\b/i, /\bshe['’]s\b/i, /\bher\b/i],
  },
  {
    role: 'third_plural',
    hint: SUBJECT_HINTS.third_plural,
    patterns: [/\bthey\b/i, /\bthey['’]re\b/i, /\bthem\b/i, /\btheir\b/i],
  },
  {
    role: 'second',
    patterns: [/\byou\b/i, /\byour\b/i, /\byours\b/i],
  },
]

const normalize = (text: string) => (text || '').replace(/\s+/g, '')

const includesAny = (normalized: string, markers: readonly string[]) =>
  markers.some(marker => normalized.includes(marker))

export const detectKoreanSubjectContext = (clause: string, isKoreanSource: boolean): SubjectContext | null => {
  if (!isKoreanSource) return null
  const normalized = normalize(clause)
  if (!normalized || !HANGUL_CHAR_RE.test(normalized)) return null
  if (includesAny(normalized, FIRST_PERSON_MARKERS)) return { role: 'first', updatedAt: Date.now() }
  if (includesAny(normalized, SECOND_PERSON_MARKERS)) return { role: 'second', updatedAt: Date.now() }
  if (includesAny(normalized, THIRD_FEM_MARKERS)) return { role: 'third_fem', hint: SUBJECT_HINTS.third_fem, updatedAt: Date.now() }
  if (includesAny(normalized, THIRD_PLURAL_MARKERS)) return { role: 'third_plural', hint: SUBJECT_HINTS.third_plural, updatedAt: Date.now() }
  if (includesAny(normalized, THIRD_MASC_MARKERS)) return { role: 'third_masc', hint: SUBJECT_HINTS.third_masc, updatedAt: Date.now() }
  return null
}

export const detectEnglishSubjectContext = (text: string): SubjectContext | null => {
  const sample = (text || '').trim()
  if (!sample) return null
  let best: SubjectContext | null = null
  let bestIndex = Number.POSITIVE_INFINITY
  ENGLISH_SUBJECT_RULES.forEach(rule => {
    rule.patterns.forEach(pattern => {
      const idx = sample.search(pattern)
      if (idx >= 0 && idx < bestIndex && idx <= 180) {
        bestIndex = idx
        best = { role: rule.role, hint: rule.hint, updatedAt: Date.now() }
      }
    })
  })
  return best
}

export const isThirdPersonRole = (
  role?: SubjectRole
): role is Extract<SubjectRole, 'third_masc' | 'third_fem' | 'third_plural'> =>
  role === 'third_masc' || role === 'third_fem' || role === 'third_plural'

const thirdPersonBase = (role: SubjectRole) => {
  if (role === 'third_fem') return 'She'
  if (role === 'third_plural') return 'They'
  return 'He'
}

const thirdPersonBePresent = (role: SubjectRole) =>
  role === 'third_plural' ? 'They are' : `${thirdPersonBase(role)} is`

const thirdPersonBePast = (role: SubjectRole) =>
  role === 'third_plural' ? 'They were' : `${thirdPersonBase(role)} was`

const thirdPersonHave = (role: SubjectRole) =>
  role === 'third_plural' ? 'They have' : `${thirdPersonBase(role)} has`

const thirdPersonWill = (role: SubjectRole) =>
  role === 'third_plural' ? 'They will' : `${thirdPersonBase(role)} will`

const thirdPersonWould = (role: SubjectRole) =>
  role === 'third_plural' ? 'They would' : `${thirdPersonBase(role)} would`

const thirdPersonCan = (role: SubjectRole) =>
  role === 'third_plural' ? 'They can' : `${thirdPersonBase(role)} can`

const thirdPersonContraction = (role: SubjectRole, type: 'am' | 'will' | 'would') => {
  if (role === 'third_plural') {
    if (type === 'am') return "They're"
    if (type === 'will') return "They'll"
    if (type === 'would') return "They'd"
  }
  if (role === 'third_fem') {
    if (type === 'am') return "She's"
    if (type === 'will') return "She'll"
    if (type === 'would') return "She'd"
  }
  if (type === 'am') return "He's"
  if (type === 'will') return "He'll"
  if (type === 'would') return "He'd"
  return undefined
}

type PronounRewriteResult = { text: string; changed: boolean }

const stripLeadingQuote = (text: string) => {
  const match = text.match(/^(\s*[“”"'‘’]?)/)
  return {
    prefix: match?.[1] ?? '',
    body: text.slice((match?.[1]?.length ?? 0)),
  }
}

export const rewriteFirstPersonWithContext = (
  english: string,
  sourceText: string,
  ctx: SubjectContext | null,
  isSourceKorean: boolean,
  ttlMs: number = SUBJECT_HINT_TTL_MS
): PronounRewriteResult | null => {
  if (!ctx || !isThirdPersonRole(ctx.role)) return null
  if (Date.now() - ctx.updatedAt > ttlMs) return null

  if (isSourceKorean && sourceText) {
    const srcCtx = detectKoreanSubjectContext(sourceText, isSourceKorean)
    if (srcCtx && (srcCtx.role === 'first' || srcCtx.role === 'second')) {
      return null
    }
  }

  const englishCtx = detectEnglishSubjectContext(english)
  if (!englishCtx || englishCtx.role !== 'first') return null

  const { prefix, body } = stripLeadingQuote(english)
  const trimmedBody = body.trimStart()
  if (!trimmedBody) return null

  const replacements: { pattern: RegExp; replacement: string }[] = [
    { pattern: /^I['’]m\b/i, replacement: thirdPersonContraction(ctx.role, 'am') ?? thirdPersonBePresent(ctx.role) },
    { pattern: /^I am\b/i, replacement: thirdPersonBePresent(ctx.role) },
    { pattern: /^I was\b/i, replacement: thirdPersonBePast(ctx.role) },
    { pattern: /^I have\b/i, replacement: thirdPersonHave(ctx.role) },
    { pattern: /^I['’]ve\b/i, replacement: thirdPersonHave(ctx.role) },
    { pattern: /^I will\b/i, replacement: thirdPersonWill(ctx.role) },
    { pattern: /^I['’]ll\b/i, replacement: thirdPersonContraction(ctx.role, 'will') ?? thirdPersonWill(ctx.role) },
    { pattern: /^I would\b/i, replacement: thirdPersonWould(ctx.role) },
    { pattern: /^I['’]d\b/i, replacement: thirdPersonContraction(ctx.role, 'would') ?? thirdPersonWould(ctx.role) },
    { pattern: /^I can\b/i, replacement: thirdPersonCan(ctx.role) },
    { pattern: /^I\b/i, replacement: thirdPersonBase(ctx.role) },
  ]

  for (const { pattern, replacement } of replacements) {
    if (pattern.test(trimmedBody)) {
      const next = trimmedBody.replace(pattern, replacement)
      const leadingWhitespace = body.match(/^\s*/)?.[0] ?? ''
      const rewritten = `${prefix}${leadingWhitespace}${next}`
      if (rewritten !== english) {
        return { text: rewritten, changed: true }
      }
      return null
    }
  }

  return null
}

const matchReplacementCase = (source: string, replacement: string) => {
  if (!source) return replacement
  if (source === source.toUpperCase()) return replacement.toUpperCase()
  if (source[0] && source[0] === source[0].toUpperCase() && source[0] !== source[0].toLowerCase()) {
    return replacement[0].toUpperCase() + replacement.slice(1)
  }
  return replacement
}

const REFLEXIVE_MAP: Record<
  Extract<SubjectRole, 'third_masc' | 'third_fem' | 'third_plural'>,
  { pattern: RegExp; replacement: string }[]
> = {
  third_masc: [
    { pattern: /\bfor myself\b/gi, replacement: 'for himself' },
    { pattern: /\bby myself\b/gi, replacement: 'by himself' },
    { pattern: /\bon my own\b/gi, replacement: 'on his own' },
  ],
  third_fem: [
    { pattern: /\bfor myself\b/gi, replacement: 'for herself' },
    { pattern: /\bby myself\b/gi, replacement: 'by herself' },
    { pattern: /\bon my own\b/gi, replacement: 'on her own' },
  ],
  third_plural: [
    { pattern: /\bfor myself\b/gi, replacement: 'for themselves' },
    { pattern: /\bby myself\b/gi, replacement: 'by themselves' },
    { pattern: /\bon my own\b/gi, replacement: 'on their own' },
  ],
}

export const rewriteReflexivePronouns = (
  english: string,
  sourceText: string,
  ctx: SubjectContext | null,
  isSourceKorean: boolean,
  ttlMs: number = SUBJECT_HINT_TTL_MS
): PronounRewriteResult | null => {
  if (!ctx || !isThirdPersonRole(ctx.role)) return null
  if (Date.now() - ctx.updatedAt > ttlMs) return null

  if (isSourceKorean && sourceText) {
    const srcCtx = detectKoreanSubjectContext(sourceText, isSourceKorean)
    if (srcCtx && (srcCtx.role === 'first' || srcCtx.role === 'second')) {
      return null
    }
  }

  const replacements = REFLEXIVE_MAP[ctx.role]
  if (!replacements) return null

  let result = english
  let changed = false
  replacements.forEach(({ pattern, replacement }) => {
    result = result.replace(pattern, match => {
      changed = true
      return matchReplacementCase(match, replacement)
    })
  })

  if (!changed || result === english) return null
  return { text: result, changed: true }
}
