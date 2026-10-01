export type ErrorClass = "archive" | "entry" | "body" | "payload" | "resolution" | "request";

export class VzipError extends Error {
  cls: ErrorClass;
  constructor(cls: ErrorClass, message: string) {
    super(message);
    this.cls = cls;
  }
}

export function fail(cls: ErrorClass, message: string): never {
  throw new VzipError(cls, message);
}
