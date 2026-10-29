// PNG.sync.read with skipRescale preserves native sample precision. The library
// applies palette/tRNS; PAM carries sub-byte grayscale's unscaled maxval.
import { readFileSync } from 'node:fs';
import { api, assert, Pam, decoderError, notInstalled } from './pam.mjs';
const { default: pngjs } = await import('pngjs').catch(notInstalled);
const [, , input, output] = process.argv;
if (input === '--version') {
	const v = JSON.parse(readFileSync(new URL('./node_modules/pngjs/package.json', import.meta.url))).version;
	console.log(`pngjs ${v} (node ${process.version})`); process.exit(0);
}
const bytes = readFileSync(input);
let png;
try { png = pngjs.PNG.sync.read(bytes, { skipRescale: true }); } catch (e) { decoderError(e); }
const { width: w, height: h, depth, data } = png;
console.log(`INFO width=${w} height=${h} depth=${depth} colorType=${png.colorType}`);
api(data.length === w * h * 4, `unexpected ${data.length} samples for ${w}x${h}`);
api(depth !== 16 || data instanceof Uint16Array, '16-bit output is not Uint16Array');
const maxval = depth === 16 ? 65535 : depth < 8 && !png.palette ? (1 << depth) - 1 : 255;
const out = new Pam(output, w, h, 4, maxval);
const row = depth === 16 ? Buffer.alloc(w * 8) : null;
for (let y = 0; y < h; y++) {
	const source = data.subarray(y * w * 4, (y + 1) * w * 4);
	if (row) {
		for (let i = 0; i < source.length; i++) row.writeUInt16BE(source[i], i * 2);
	}
	if (maxval < 255) { const over = source.find(v => v > maxval); api(over === undefined, `sample ${over} past bit depth ${depth}`); }
	out.row(row || source);
}
out.close();
console.log(`OK ${w} ${h} 4 ${depth === 16 ? 16 : 8}`);
