import { afterEach, describe, expect, test, vi } from 'vitest';
import { ApiError, api, errorMessage } from './api';

/* What a failed request says to the person. FastAPI's validation errors arrive
   as a list of {loc, msg} objects, which stringified read "[object Object]". */

describe('error messages', () => {
  test('a plain detail string is the message', () => {
    expect(errorMessage('{"detail":"A service named \'x\' already exists"}', 409))
      .toBe("A service named 'x' already exists");
  });

  test('a 422 list of validation errors becomes one readable line per field', () => {
    const body = JSON.stringify({ detail: [
      { loc: ['body', 'stop_timeout_seconds'], msg: 'Input should be greater than or equal to 1', type: 'greater_than_equal' },
      { loc: ['body', 'windows', 0, 'start'], msg: 'String should match pattern', type: 'string_pattern_mismatch' },
    ] });
    expect(errorMessage(body, 422))
      .toBe('stop_timeout_seconds: Input should be greater than or equal to 1; windows.0.start: String should match pattern');
  });

  test('a body that is not JSON, or an empty one, still says something', () => {
    expect(errorMessage('<html>Bad gateway</html>', 502)).toBe('<html>Bad gateway</html>');
    expect(errorMessage('', 500)).toBe('Request failed: 500');
    expect(errorMessage('{"detail":{"odd":true}}', 422)).toBe('{"odd":true}');
  });
});

describe('api()', () => {
  afterEach(() => vi.unstubAllGlobals());

  test('a failed request throws an ApiError that keeps the status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"Service not found"}', { status: 404 })));
    const error = await api('/services/9').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toBeInstanceOf(Error);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).message).toBe('Service not found');
  });

  test('a 204 resolves to nothing', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })));
    await expect(api('/services/9', { method: 'DELETE' })).resolves.toBeUndefined();
  });
});
