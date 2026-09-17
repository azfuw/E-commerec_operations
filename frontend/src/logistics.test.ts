import { describe, expect, it } from 'vitest'

import { formatShanghai, overviewEventDescription, shanghaiInputValue, toCsv, toUtcIso } from './logistics'

describe('logistics helpers', () => {
  it('formats UTC timestamps in Asia/Shanghai', () => {
    expect(formatShanghai('2026-09-17T00:00:00Z')).toBe('2026/09/17 08:00')
    expect(formatShanghai(null)).toBe('\u2014')
  })

  it('round-trips Beijing datetime inputs and advances to the next whole second', () => {
    expect(shanghaiInputValue(new Date('2026-09-17T00:00:00.750Z'))).toBe('2026-09-17T08:00:01')
    expect(toUtcIso('2026-09-17T08:00:01')).toBe('2026-09-17T00:00:01.000Z')
  })

  it('exports spreadsheet-safe UTF-8 CSV with escaped values', () => {
    expect(
      toCsv(
        ['\u8ba2\u5355', '\u8bf4\u660e'],
        [
          ['=2+2', 'a,b'],
          ['ORD-2', 'a"b'],
          ['\t=cmd', 'safe'],
        ],
      ),
    ).toBe('\ufeff\u8ba2\u5355,\u8bf4\u660e\r\n\'=2+2,"a,b"\r\nORD-2,"a""b"\r\n\'\t=cmd,safe')
  })

  it('hides only a generated task UUID suffix in overview activity', () => {
    expect(overviewEventDescription('规则巡检发现发货超时：超过承诺时间。 任务 35493d90-4770-4826-b2ef-09b6ea2c884b。')).toBe(
      '规则巡检发现发货超时：超过承诺时间。',
    )
    expect(overviewEventDescription('异常任务 35493d90-4770-4826-b2ef-09b6ea2c884b（轨迹停滞）指派给 logistics。')).toBe(
      '异常工单（轨迹停滞）指派给 logistics。',
    )
    expect(overviewEventDescription('人工备注：任务 VIP-8 已联系。')).toBe('人工备注：任务 VIP-8 已联系。')
  })
})
