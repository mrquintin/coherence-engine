/**
 * Frontend smoke / unit-level coverage for the partner dashboard
 * pipeline view. The Playwright runner picks up ``*.spec.ts`` files
 * but the partner dashboard does not run a real backend in CI — so
 * the assertions here are unit-style: URL filter parsing, override
 * form validation, and a smoke-load of the unauthenticated /pipeline
 * shell. Full e2e (against a live FastAPI + seeded DB) is layered in
 * a follow-up CI job.
 */

import { test, expect } from '@playwright/test';
import {
  parsePipelineFilter,
  serializePipelineFilter,
  KNOWN_VERDICTS,
} from '../src/lib/pipeline_filter';
import {
  validateOverrideForm,
  MIN_REASON_TEXT_LENGTH,
} from '../src/app/applications/[id]/override/validate';

test.describe('pipeline filter URL parser', () => {
  test('returns defaults for an empty search-params object', () => {
    const f = parsePipelineFilter({});
    expect(f.domain).toBe('');
    expect(f.verdict).toBe('');
    expect(f.mode).toBe('');
    expect(f.cursor).toBe('');
    expect(f.limit).toBe(25);
  });

  test('accepts known verdict + mode values', () => {
    for (const v of KNOWN_VERDICTS) {
      const f = parsePipelineFilter({ verdict: v });
      expect(f.verdict).toBe(v);
    }
    expect(parsePipelineFilter({ mode: 'shadow' }).mode).toBe('shadow');
    expect(parsePipelineFilter({ mode: 'enforce' }).mode).toBe('enforce');
  });

  test('rejects unknown verdict / mode silently', () => {
    expect(parsePipelineFilter({ verdict: 'bogus' }).verdict).toBe('');
    expect(parsePipelineFilter({ mode: 'bogus' }).mode).toBe('');
  });

  test('clamps limit to MAX_LIMIT', () => {
    expect(parsePipelineFilter({ limit: '500' }).limit).toBe(100);
    expect(parsePipelineFilter({ limit: 'not-a-number' }).limit).toBe(25);
    expect(parsePipelineFilter({ limit: '50' }).limit).toBe(50);
  });

  test('serializes round-trip cleanly', () => {
    const qs = serializePipelineFilter({
      domain: 'market_economics',
      verdict: 'reject',
      mode: 'enforce',
      cursor: 'app_x',
      limit: 50,
    });
    expect(qs).toContain('domain=market_economics');
    expect(qs).toContain('verdict=reject');
    expect(qs).toContain('mode=enforce');
    expect(qs).toContain('cursor=app_x');
    expect(qs).toContain('limit=50');
  });

  test('omits default limit from serialization', () => {
    const qs = serializePipelineFilter({ domain: 'x', limit: 25 });
    expect(qs).not.toContain('limit=');
  });
});

test.describe('override form validation', () => {
  const goodText = 'a'.repeat(MIN_REASON_TEXT_LENGTH);

  test('accepts a complete payload', () => {
    const result = validateOverrideForm({
      override_verdict: 'manual_review',
      reason_code: 'manual_diligence',
      reason_text: goodText,
      justification_uri: '',
      unrevise: false,
    });
    expect(result.ok).toBe(true);
  });

  test('rejects unknown verdict', () => {
    const result = validateOverrideForm({
      override_verdict: 'bogus',
      reason_code: 'manual_diligence',
      reason_text: goodText,
      justification_uri: '',
      unrevise: false,
    });
    expect(result.ok).toBe(false);
  });

  test('rejects unknown reason_code', () => {
    const result = validateOverrideForm({
      override_verdict: 'manual_review',
      reason_code: 'wat',
      reason_text: goodText,
      justification_uri: '',
      unrevise: false,
    });
    expect(result.ok).toBe(false);
  });

  test('rejects short reason_text', () => {
    const result = validateOverrideForm({
      override_verdict: 'manual_review',
      reason_code: 'manual_diligence',
      reason_text: 'too short',
      justification_uri: '',
      unrevise: false,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error).toContain('40');
    }
  });

  test('requires memo for reject override', () => {
    const result = validateOverrideForm({
      override_verdict: 'reject',
      reason_code: 'factual_error',
      reason_text: goodText,
      justification_uri: '',
      unrevise: false,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.toLowerCase()).toContain('memo');
    }
  });

  test('accepts reject + memo', () => {
    const result = validateOverrideForm({
      override_verdict: 'reject',
      reason_code: 'factual_error',
      reason_text: goodText,
      justification_uri: 's3://memos/x.pdf',
      unrevise: false,
    });
    expect(result.ok).toBe(true);
  });
});
