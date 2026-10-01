// Error classes of spec §8.4.

export type ErrorClass = "archive" | "entry" | "body" | "payload" | "resolution" | "request";

export class VzError extends Error {
  cls: ErrorClass;
  constructor(cls: ErrorClass, message: string) {
    super(message);
    this.cls = cls;
    this.name = "VzError";
  }
}

/** Thrown by protobuf decoding; callers convert to the right class. */
export class MalformedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MalformedError";
  }
}

/** Thrown by the writer for invalid input (spec §9.1, HARNESS.md). */
export class InvalidInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InvalidInputError";
  }
}

export const archiveErr = (m: string) => new VzError("archive", m);
export const entryErr = (m: string) => new VzError("entry", m);
export const bodyErr = (m: string) => new VzError("body", m);
export const payloadErr = (m: string) => new VzError("payload", m);
export const resolutionErr = (m: string) => new VzError("resolution", m);
export const requestErr = (m: string) => new VzError("request", m);
