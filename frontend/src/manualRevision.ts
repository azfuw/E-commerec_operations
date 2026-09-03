import type {
  AttributeCompletion,
  DescriptionSection,
  ManualRevisionRequest,
  OptimizationChange,
  ProposalDetail,
} from './types'

export type EditableProposalField =
  | 'title'
  | 'selling_points'
  | 'description'
  | 'keywords'
  | 'attribute_completions'

export type ManualRevisionFormValue = {
  title: string
  selling_points: string[]
  description: DescriptionSection[]
  keywords: string[]
  attribute_completions: Array<{
    target_attribute: string
    suggested_value: string
  }>
}

export type ManualRevisionBuildResult = {
  request: ManualRevisionRequest | null
  unsupported: EditableProposalField[]
}

function hasCurrentValue(value: OptimizationChange['current_value']): boolean {
  return typeof value === 'string' ? value.length > 0 : value.length > 0
}

export function buildManualRevisionRequest(
  detail: ProposalDetail,
  form: ManualRevisionFormValue,
): ManualRevisionBuildResult {
  const revision = detail.current_revision
  if (!revision) {
    return {
      request: null,
      unsupported: ['title', 'selling_points', 'description', 'keywords', 'attribute_completions'],
    }
  }

  const output = revision.proposal_output
  const unsupported: EditableProposalField[] = []
  const changes: OptimizationChange[] = []
  const contentFields = ['title', 'selling_points', 'description', 'keywords'] as const
  const values = {
    title: form.title,
    selling_points: form.selling_points,
    description: form.description,
    keywords: form.keywords,
  }
  const content = {
    title: output.title,
    selling_points: output.selling_points,
    description: output.description,
    keywords: output.keywords,
  }

  for (const field of contentFields) {
    const parent = output.changes.find((change) => change.field === field)
    if (!parent || !hasCurrentValue(parent.current_value) || !parent.reason.trim() || !parent.evidence.length) {
      unsupported.push(field)
      continue
    }
    content[field] = values[field]
    changes.push({ ...parent, suggested_value: values[field] })
  }

  const parentAttributes = new Map(
    output.attribute_completions.map((item) => [item.target_attribute, item]),
  )
  const attributeCompletions: AttributeCompletion[] = []
  for (const value of form.attribute_completions) {
    const parent = parentAttributes.get(value.target_attribute)
    if (!parent?.evidence.length) {
      if (!unsupported.includes('attribute_completions')) unsupported.push('attribute_completions')
      continue
    }
    attributeCompletions.push({ ...parent, suggested_value: value.suggested_value })
  }

  return {
    request: {
      parent_revision_id: revision.id,
      base_product_version: revision.base_product_version,
      ...content,
      attribute_completions: attributeCompletions,
      changes,
    },
    unsupported,
  }
}
