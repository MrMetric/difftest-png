// Row-bounded output. Three exits besides success: a decoder error (ERR, 1), a library result
// that is not an image the shim can write (API, 71), the adapter's own failure (SHIM, 70).
import assert from 'node:assert/strict';
import { openSync, writeSync, closeSync } from 'node:fs';
export { assert };
function describe(error) {
	const parts = [], seen = new Set();
	while (error != null && !seen.has(error)) {
		seen.add(error);
		parts.push(error instanceof Error ? `${error.name}: ${error.message}` : String(error));
		error = error.cause;
	}
	return parts.join(' <- ').replace(/[\r\n]/g, ' ');
}
export function decoderError(error) {
	console.log('ERR ' + describe(error)); process.exit(1);
}
export function shimError(error) {
	console.log('SHIM ' + describe(error)); process.exit(70);
}
export function notInstalled(error) {
	if (error.code !== 'ERR_MODULE_NOT_FOUND' && error.code !== 'MODULE_NOT_FOUND') shimError(error);
	console.log('ERR decoder library not installed: ' + error.message); process.exit(127);
}
export class ApiError extends Error { name = 'ApiError'; }
export function api(ok, message) { if (!ok) throw new ApiError(message); }
export function apiError(error) {
	console.log('API ' + error.message.replace(/[\r\n]/g, ' ')); process.exit(71);
}
process.on('uncaughtException', e => e instanceof ApiError ? apiError(e) : shimError(e));
export class Pam {
	constructor(path, width, height, channels, maxval) {
		api(Number.isSafeInteger(width) && width > 0 && Number.isSafeInteger(height) && height > 0, `invalid image dimensions ${width}x${height}`);
		assert(channels >= 1 && channels <= 4 && maxval >= 1 && maxval <= 65535, `invalid output channels/precision: ${channels}/${maxval}`);
		this.rowBytes = width * channels * (maxval > 255 ? 2 : 1);
		assert(Number.isSafeInteger(this.rowBytes), 'output row size exceeds safe integer range');
		this.height = height; this.rows = 0;
		this.fd = openSync(path, 'w');
		const type = ['GRAYSCALE', 'GRAYSCALE_ALPHA', 'RGB', 'RGB_ALPHA'][channels - 1];
		this.writeAll(Buffer.from(`P7\nWIDTH ${width}\nHEIGHT ${height}\nDEPTH ${channels}\nMAXVAL ${maxval}\nTUPLTYPE ${type}\nENDHDR\n`));
	}
	writeAll(data) {
		for (let off = 0; off < data.length;) {
			const n = writeSync(this.fd, data, off, data.length - off);
			assert(n > 0, 'zero-length output write'); off += n;
		}
	}
	row(data) {
		api(this.rows < this.height, `more rows than the image height ${this.height}`);
		api(data.length === this.rowBytes, `row ${this.rows}: ${data.length} bytes, expected ${this.rowBytes}`);
		this.writeAll(data); this.rows++;
	}
	close() {
		api(this.rows === this.height, `${this.rows} rows, expected ${this.height}`);
		closeSync(this.fd);
	}
}
