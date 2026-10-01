export type ErrorClass =
  | "archive"
  | "entry"
  | "body"
  | "payload"
  | "resolution"
  | "request";

/** An error of one of the six classes of spec §8.4. */
export class VzipError extends Error {
  errorClass: ErrorClass;
  constructor(errorClass: ErrorClass, message: string) {
    super(message);
    this.errorClass = errorClass;
    this.name = "VzipError";
  }
}

/** Thrown by the protobuf decoder for a malformed message (§5.1). */
export class MalformedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MalformedError";
  }
}

/** Thrown by the writer when its input must be rejected (§9.1). */
export class WriterInputError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WriterInputError";
  }
}
