// UPNG.decode then UPNG.toRGBA8, first frame: for an APNG that is the first
// animation frame, UPNG's choice. toRGBA8 is 8-bit only (max_depth 8).
import { readFileSync } from 'node:fs';
import { api, assert, Pam, decoderError, notInstalled } from './pam.mjs';
const { default: UPNG } = await import('upng-js').catch(notInstalled);
const [, , input, output] = process.argv;
if (input === '--version') {
	const v = JSON.parse(readFileSync(new URL('./node_modules/upng-js/package.json', import.meta.url))).version;
	console.log(`upng-js ${v} (node ${process.version})`); process.exit(0);
}
const bytes = readFileSync(input);
let img, frames;
try {
	img = UPNG.decode(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
	frames = UPNG.toRGBA8(img);
} catch (e) { decoderError(e); }
const { width: w, height: h } = img;
console.log(`INFO width=${w} height=${h} depth=${img.depth} ctype=${img.ctype} frames=${frames.length}`);
api(frames.length > 0, 'no frames');
const body = Buffer.from(frames[0]); // ArrayBuffer view, not a copy.
api(body.length === w * h * 4, `unexpected frame length ${body.length} for ${w}x${h}`);
const out = new Pam(output, w, h, 4, 255);
for (let y = 0; y < h; y++) out.row(body.subarray(y * w * 4, (y + 1) * w * 4));
out.close();
console.log(`OK ${w} ${h} 4 8`);
