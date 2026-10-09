// The Rust core (rust/vzip-ir) as a WebAssembly instance: compiled and
// instantiated once, lazily, and shared by every run. Runs are pointers into its
// memory, so several can be in progress at once; JavaScript calls into it one at
// a time, and each call's result (at `vz_out()`) is read before the next call.
// Memory can grow during any call, so views of it are made after each call.

import { compileIr } from "#irwasm";

/** The C-ABI of rust/vzip-ir/src/wasm.rs that the runs use. */
export interface IrExports {
  memory: WebAssembly.Memory;
  vz_alloc(n: number): number;
  vz_out(): number;
  vz_run_new(format: number, size: bigint, remote: number, concurrency: number): number;
  vz_run_seed(p: number, offset: bigint, data: number, n: number): void;
  vz_run_observe(p: number, n: bigint, seconds: number): void;
  vz_run_multirange(p: number, v: number): void;
  vz_run_settings(p: number, byteCost: number, wholeBelow: bigint): void;
  vz_run_poll(p: number): bigint;
  vz_run_complete(p: number, k: number, ok: number, data: number, n: number, seconds: number): bigint;
  vz_run_stats(p: number): bigint;
  vz_run_output(p: number, url: number, n: number, mirror: number): bigint;
  vz_run_free(p: number): void;
}

let instance: Promise<IrExports> | undefined;

/** The shared instance (instantiated on first use; a failure is retried on the next). */
export function ir(): Promise<IrExports> {
  instance ??= (async () => {
    const module = await compileIr();
    const { exports } = await WebAssembly.instantiate(module, {});
    return exports as unknown as IrExports;
  })();
  instance.catch(() => (instance = undefined));
  return instance;
}

/** Copies `bytes` into memory the core allocates (and takes ownership of): its pointer. */
export function put(w: IrExports, bytes: Uint8Array): number {
  const p = w.vz_alloc(bytes.length) >>> 0;
  new Uint8Array(w.memory.buffer, p, bytes.length).set(bytes);
  return p;
}

/** A copy of the `n` bytes of the last call's result. */
export function out(w: IrExports, n: number): Uint8Array {
  return new Uint8Array(w.memory.buffer, w.vz_out() >>> 0, n).slice();
}
