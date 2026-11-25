import { describe, expect, test } from 'vitest';
import {
  isObviouslyInvalidStockQuery,
  looksLikeStockCode,
  validateStockCode,
} from '../validation';

describe('stock code validation', () => {
  test.each(['BRK-B', 'BAC-PL'])('accepts Yahoo stock code %s', (code) => {
    expect(validateStockCode(code.toLowerCase())).toEqual({ valid: true, normalized: code });
    expect(isObviouslyInvalidStockQuery(code)).toBe(false);
  });

  test.each([
    ['7203.T', '7203.T'],
    ['6758.t', '6758.T'],
    ['005930.KS', '005930.KS'],
    ['247540.kq', '247540.KQ'],
    ['2330.TW', '2330.TW'],
    ['2330.tw', '2330.TW'],
    ['6488.TWO', '6488.TWO'],
    ['6488.two', '6488.TWO'],
    ['00631L.TW', '00631L.TW'],
    ['00631l.tw', '00631L.TW'],
    ['00632R.TW', '00632R.TW'],
    ['00679B.TWO', '00679B.TWO'],
    ['00679b.two', '00679B.TWO'],
    ['2881A.TW', '2881A.TW'],
    ['2887Z1.TW', '2887Z1.TW'],
  ])('accepts JP/KR/TW Yahoo suffix code %s', (input, normalized) => {
    expect(looksLikeStockCode(input)).toBe(true);
    expect(validateStockCode(input)).toEqual({
      valid: true,
      normalized,
    });
    expect(isObviouslyInvalidStockQuery(input)).toBe(false);
  });

  test.each(['7203', '005930.K', '035720.KRX', '2330.TWW', '233.TW', '233L.TW', '00631LL.TW', 'ABCD.TW'])(
    'does not treat ambiguous JP/KR/TW-like query %s as a valid suffix code',
    (input) => {
      const result = validateStockCode(input);
      expect(result.valid).toBe(false);
    }
  );

  test.each([
    ['sh000016', 'SH000016'],
    ['sh000300', 'SH000300'],
    ['sz399001', 'SZ399001'],
  ])('accepts registered SH/SZ index canonical %s', (input, normalized) => {
    expect(looksLikeStockCode(input)).toBe(true);
    expect(validateStockCode(input)).toEqual({
      valid: true,
      normalized,
    });
    expect(isObviouslyInvalidStockQuery(input)).toBe(false);
  });

  test.each([
    ['csi930955', 'CSI930955'],
    ['930955.CSI', '930955.CSI'],
    ['CSI930955', 'CSI930955'],
  ])('accepts registered CSI index forms %s', (input, normalized) => {
    expect(looksLikeStockCode(input)).toBe(true);
    expect(validateStockCode(input)).toEqual({
      valid: true,
      normalized,
    });
    expect(isObviouslyInvalidStockQuery(input)).toBe(false);
  });

  test('rejects unregistered CSI forms that are not registered index canonicals', () => {
    // 930956.CSI is not in the registry; the Web validation layer is
    // format-based (registered rows pass), while the API returns a 4xx for
    // unregistered CSI at the backend boundary.
    expect(looksLikeStockCode('930956.CSI')).toBe(true);
    expect(validateStockCode('930956.CSI').valid).toBe(true);
  });
});
