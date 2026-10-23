// UPNG.js shim. Policy: UPNG.decode then UPNG.toRGBA8, first frame. toRGBA8
// is 8-bit only (16-bit sources keep the high byte), so this column is
// registered with max_depth = 8.
import { readFileSync, writeFileSync } from 'node:fs';
// 127: the decoder library is not installed (see impls/CONTRACT.md).
const notInstalled = (e) => { console.log('ERR decoder library not installed: ' + e.message); process.exit(127); };
const { default: UPNG } = await import('upng-js').catch(notInstalled);

const [, , input, output] = process.argv;
if (input === '--version') {
	const v = JSON.parse(readFileSync(new URL('./node_modules/upng-js/package.json', import.meta.url))).version;
	console.log(`upng-js ${v} (node ${process.version})`);
	process.exit(0);
}
const fail = (m) => { console.log('ERR ' + m); process.exit(1); };
let img, frames;
try {
	const buf = readFileSync(input);
	img = UPNG.decode(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
	frames = UPNG.toRGBA8(img);
} catch (e) { fail(e && e.message ? e.message : String(e)); }
const { width: w, height: h } = img;
console.log(`INFO width=${w} height=${h} depth=${img.depth} ctype=${img.ctype} frames=${frames.length}`);
if (!frames.length) fail('no frames');
const body = Buffer.from(frames[0]);
if (body.length !== w * h * 4) fail(`unexpected frame length ${body.length} for ${w}x${h}`);
writeFileSync(output, Buffer.concat([Buffer.from(`P7\nWIDTH ${w}\nHEIGHT ${h}\nDEPTH 4\nMAXVAL 255\nTUPLTYPE RGB_ALPHA\nENDHDR\n`), body]));
console.log(`OK ${w} ${h} 4 8`);
