export type ErrorClass = "archive" | "entry" | "body" | "payload" | "resolution" | "request";

export class VzError extends Error {
  cls: ErrorClass;
  constructor(cls: ErrorClass, message: string) {
    super(message);
    this.cls = cls;
  }
}

/** Thrown by the protobuf decoder; callers map it to the right error class. */
export class MalformedError extends Error {}

/** Thrown by the writer for invalid input. */
export class InputError extends Error {}

const utf8Fatal = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/** Decode UTF-8 strictly, keeping a leading BOM. Returns null if invalid. */
export function decodeUtf8(b: Uint8Array): string | null {
  try {
    return utf8Fatal.decode(b);
  } catch {
    return null;
  }
}
