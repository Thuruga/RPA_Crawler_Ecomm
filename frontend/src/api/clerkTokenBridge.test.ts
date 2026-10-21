import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  getClerkToken,
  notifyUnauthorized,
  resetClerkTokenBridge,
  setClerkTokenGetter,
  setUnauthorizedHandler,
} from './clerkTokenBridge';

describe('clerkTokenBridge', () => {
  afterEach(() => {
    resetClerkTokenBridge();
  });

  it('resolves null before any getter is registered', async () => {
    const token = await getClerkToken();
    expect(token).toBeNull();
  });

  it('resolves value returned by registered token getter', async () => {
    setClerkTokenGetter(async () => 'session-token-xyz');
    const token = await getClerkToken();
    expect(token).toBe('session-token-xyz');
  });

  it('resolves null if token getter throws or rejects', async () => {
    setClerkTokenGetter(async () => {
      throw new Error('Clerk not loaded');
    });
    const token = await getClerkToken();
    expect(token).toBeNull();
  });

  it('notifyUnauthorized is a no-op by default', () => {
    expect(() => notifyUnauthorized(false)).not.toThrow();
    expect(() => notifyUnauthorized(true)).not.toThrow();
  });

  it('calls registered unauthorized handler with hadToken flag', () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    notifyUnauthorized(true);
    expect(handler).toHaveBeenCalledWith(true);

    notifyUnauthorized(false);
    expect(handler).toHaveBeenCalledWith(false);
  });

  it('resetClerkTokenBridge restores default null getter and no-op handler', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    setClerkTokenGetter(async () => 'temp-token');

    resetClerkTokenBridge();

    expect(await getClerkToken()).toBeNull();
    notifyUnauthorized(true);
    expect(handler).not.toHaveBeenCalled();
  });
});
