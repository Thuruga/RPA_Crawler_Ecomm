export type TokenGetter = () => Promise<string | null>;
export type UnauthorizedHandler = (hadToken: boolean) => void;

let tokenGetter: TokenGetter = async () => null;
let unauthorizedHandler: UnauthorizedHandler = () => {};

export function setClerkTokenGetter(getter: TokenGetter): void {
  tokenGetter = getter;
}

export function getClerkToken(): Promise<string | null> {
  return Promise.resolve()
    .then(() => tokenGetter())
    .catch(() => null);
}

export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  unauthorizedHandler = handler;
}

export function notifyUnauthorized(hadToken: boolean): void {
  unauthorizedHandler(hadToken);
}

export function resetClerkTokenBridge(): void {
  tokenGetter = async () => null;
  unauthorizedHandler = () => {};
}
