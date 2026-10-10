// A worker decoding JPEG 2000 codestreams for the map page (jpeg2k.ts).

import { decodeJpeg2000 } from "./jpeg2k_decoder.ts";

interface Request {
  id: number;
  wasm: string;
  bytes: Uint8Array;
  bytesPerSample: 1 | 2;
  signed: boolean;
}

const scope = self as unknown as DedicatedWorkerGlobalScope;
scope.onmessage = async ({ data }: MessageEvent<Request>) => {
  try {
    const d = await decodeJpeg2000(data.wasm, data.bytes, data.bytesPerSample, data.signed);
    scope.postMessage({ id: data.id, decoded: d }, [d.data.buffer]);
  } catch (e) {
    scope.postMessage({ id: data.id, error: String((e as Error).message ?? e) });
  }
};
