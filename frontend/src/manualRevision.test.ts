import { describe, expect, it } from 'vitest'

import { buildManualRevisionRequest } from './manualRevision'
import type { ManualRevisionFormValue } from './manualRevision'
import type { ProposalDetail } from './types'

const fact = (value: string) => ({ kind: 'fact' as const, value })
const citation = (value: string) => ({ kind: 'citation' as const, value })

function detail(): ProposalDetail {
  return {
    proposal: {
      id: 'proposal-1',
      analysis_run_id: 'analysis-1',
      analysis_candidate_id: 'candidate-1',
      optimization_run_id: 'optimization-1',
      store_id: 'store-1',
      product_id: 'product-1',
      base_product_version: 7,
      current_revision_id: 'revision-2',
      created_at: '2026-09-01T08:00:00Z',
      updated_at: '2026-09-01T09:00:00Z',
    },
    optimization_run: {
      id: 'optimization-1',
      workflow_type: 'optimization',
      status: 'pending_manual',
      quality_status: 'normal',
      error_code: null,
    },
    current_revision: {
      id: 'revision-2',
      iteration: 1,
      revision_number: 2,
      origin: 'agent',
      created_by: 'operator-1',
      parent_revision_id: 'revision-1',
      base_product_version: 7,
      proposal_output: {
        title: '父版本标题',
        selling_points: ['父版本卖点'],
        description: [
          { heading: '商品详情', body: '父版本详情', evidence: [citation('chunk-1')] },
        ],
        keywords: ['父版本关键词'],
        attribute_completions: [
          {
            target_attribute: '材质',
            current_value: '棉',
            suggested_value: '精梳棉',
            reason: '补全材质',
            evidence: [fact('product.attributes.材质')],
          },
        ],
        changes: [
          {
            field: 'title',
            current_value: '原商品标题',
            suggested_value: '父版本标题',
            reason: '优化标题表达',
            evidence: [fact('product.title')],
          },
          {
            field: 'selling_points',
            current_value: ['原卖点'],
            suggested_value: ['父版本卖点'],
            reason: '优化卖点表达',
            evidence: [fact('product.selling_points')],
          },
          {
            field: 'description',
            current_value: '原始详情',
            suggested_value: [
              { heading: '商品详情', body: '父版本详情', evidence: [citation('chunk-1')] },
            ],
            reason: '优化详情表达',
            evidence: [citation('chunk-1')],
          },
          {
            field: 'keywords',
            current_value: ['原关键词'],
            suggested_value: ['父版本关键词'],
            reason: '优化搜索词',
            evidence: [fact('product.search_keywords')],
          },
        ],
        citations: [{ chunk_id: 'chunk-1' }],
        price_suggestions: [
          {
            target_sku_id: 'sku-1',
            current_price: '100.00',
            suggested_price: '90.00',
            reason: '价格建议',
            evidence: [citation('chunk-1')],
          },
        ],
        sku_suggestions: [
          {
            target_sku_id: 'sku-1',
            current_code: 'SKU-RED',
            current_spec: { 颜色: '红' },
            suggested_code: 'SKU-RED-NEW',
            suggested_spec: { 颜色: '红色' },
            reason: 'SKU 建议',
            evidence: [citation('chunk-1')],
          },
        ],
      },
      citations: [
        {
          document_id: 'document-1',
          version_id: 'version-1',
          chunk_id: 'chunk-1',
          document_name: '通用规则',
          version_number: 1,
          category: '通用规则',
          canonical_text: '仅展示可信商品事实',
          active: true,
          applicable: true,
        },
      ],
    },
    current_review: null,
    active_manual_review: null,
    submitted_revision: null,
    latest_action: null,
    publish_record: null,
  }
}

function form(): ManualRevisionFormValue {
  return {
    title: '人工修订标题',
    selling_points: ['人工修订卖点'],
    description: [
      { heading: '商品详情', body: '人工修订详情', evidence: [citation('chunk-1')] },
    ],
    keywords: ['人工关键词'],
    attribute_completions: [{ target_attribute: '材质', suggested_value: '长绒棉' }],
  }
}

describe('buildManualRevisionRequest', () => {
  it('reuses only trusted parent change metadata without mutating inputs', () => {
    const parent = detail()
    const values = form()
    const originalParent = structuredClone(parent)
    const originalValues = structuredClone(values)

    const result = buildManualRevisionRequest(parent, values)

    expect(result.unsupported).toEqual([])
    expect(result.request?.parent_revision_id).toBe('revision-2')
    expect(result.request?.base_product_version).toBe(7)
    expect(result.request?.changes[0]).toMatchObject({
      field: 'title',
      current_value: '原商品标题',
      suggested_value: '人工修订标题',
      reason: '优化标题表达',
      evidence: [{ kind: 'fact', value: 'product.title' }],
    })
    expect(result.request?.attribute_completions).toEqual([
      {
        target_attribute: '材质',
        current_value: '棉',
        suggested_value: '长绒棉',
        reason: '补全材质',
        evidence: [{ kind: 'fact', value: 'product.attributes.材质' }],
      },
    ])
    expect(result.request).not.toHaveProperty('citations')
    expect(result.request).not.toHaveProperty('price_suggestions')
    expect(result.request).not.toHaveProperty('sku_suggestions')
    expect(parent).toEqual(originalParent)
    expect(values).toEqual(originalValues)
  })

  it('marks fields readonly when trusted current values, reasons, or evidence are absent', () => {
    const parent = detail()
    const output = parent.current_revision!.proposal_output
    output.changes[0]!.evidence = []
    output.changes[1]!.current_value = []
    output.changes[2]!.reason = ''

    const result = buildManualRevisionRequest(parent, form())

    expect(result.unsupported).toEqual(['title', 'selling_points', 'description'])
    expect(result.request?.changes.map((change) => change.field)).toEqual(['keywords'])
    expect(result.request?.title).toBe('父版本标题')
    expect(result.request?.selling_points).toEqual(['父版本卖点'])
    expect(result.request?.description).toEqual(output.description)
  })

  it('does not add an attribute completion without parent evidence', () => {
    const values = form()
    values.attribute_completions.push({
      target_attribute: '产地',
      suggested_value: '中国',
    })

    const result = buildManualRevisionRequest(detail(), values)

    expect(result.unsupported).toEqual(['attribute_completions'])
    expect(result.request?.attribute_completions.map((item) => item.target_attribute)).toEqual([
      '材质',
    ])
  })
})
